# -*- coding: utf-8 -*-
import os
import json
import asyncio
import logging
from pathlib import Path

# Try to load dotenv for local development, but don't fail if missing in production
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # In Cloud Run, environment variables are provided by the platform
    load_dotenv = None

from google.genai.types import Content, Part
from google.adk.runners import InMemoryRunner
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from story_agent.story_text_agent import story_agent
from story_agent.story_image_agent import DirectImageAgent

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Application constants
APP_NAME = "storygen_app"

# Initialize FastAPI app
app = FastAPI(title="StoryGen Backend", description="ADK-powered story generation backend")

# Add CORS middleware to allow frontend connections
# Get frontend URL from environment variable for production
frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
allowed_origins = [
    "http://localhost:3000", 
    "http://127.0.0.1:3000",  # Local development
    frontend_url,  # Production frontend
    "*"  # Allow all origins for WebSocket connections (more permissive for demos)
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize session service and agents
project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT_ID")
logger.info(f"🔧 Project ID: {project_id}")

# Debug: Check API key availability
api_key = os.getenv("GOOGLE_API_KEY")
use_vertexai = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
logger.info(f"🔑 API Key available: {'Yes' if api_key else 'No'}")
logger.info(f"🔑 API Key length: {len(api_key) if api_key else 0}")
logger.info(f"🎯 Using Vertex AI: {use_vertexai}")

# Check story_agent initialization
if story_agent:
    logger.info("✅ StoryAgent imported successfully")
    logger.info(f"📋 StoryAgent model: {getattr(story_agent, 'model', 'Unknown')}")
else:
    logger.error("❌ StoryAgent is None! Check story_text_agent.py import")

# Initialize direct image agent
direct_image_agent = None
if project_id:
    try:
        logger.info(f"🎨 Initializing DirectImageAgent with project: {project_id}")
        direct_image_agent = DirectImageAgent(project_id=project_id)
        logger.info("✅ Architecture initialized: StoryAgent + DirectImageAgent")
    except Exception as e:
        logger.warning(f"⚠️ Could not initialize DirectImageAgent: {e}")
        logger.info("📖 Story generation will work, but images will be disabled")
else:
    logger.info("💡 To enable image generation, set GOOGLE_CLOUD_PROJECT_ID in your .env file")


async def run_two_agent_workflow(websocket: WebSocket, user_id: str, keywords: str):
    """
    Clean two-agent workflow:
    1. StoryAgent generates structured story with scene data
    2. ImageAgent generates images for each scene
    3. Stream results to frontend as they're ready
    """
    logger.info(f"🚀 Starting two-agent workflow for user {user_id} with keywords: '{keywords}'")
    
    # Step 1: Generate structured story using StoryAgent
    story_data = None
    try:
        logger.info(f"📖 Generating story with StoryAgent for keywords: '{keywords}'")
        
        # Check if story_agent is available
        if not story_agent:
            raise Exception("StoryAgent not initialized")
        logger.info("✅ StoryAgent is available")
        
        # Create runner and session
        logger.info("🏗️ Creating InMemoryRunner...")
        story_runner = InMemoryRunner(app_name=APP_NAME, agent=story_agent)
        logger.info("📝 Creating story session...")
        story_session = await story_runner.session_service.create_session(app_name=APP_NAME, user_id=f"{user_id}_story")
        logger.info(f"✅ Story session created: {story_session.id}")
        
        # Prepare content
        story_content = Content(role="user", parts=[Part(text=f"Keywords: {keywords}")])
        logger.info(f"📨 Sending content to StoryAgent: 'Keywords: {keywords}'")

        # Run the agent
        logger.info("🤖 Starting StoryAgent execution...")
        story_response = ""
        event_count = 0
        
        try:
            logger.info(f"🔧 Environment check - API Key available: {'Yes' if os.getenv('GOOGLE_API_KEY') else 'No'}")
            logger.info(f"🔧 Environment check - Use VertexAI: {os.getenv('GOOGLE_GENAI_USE_VERTEXAI', 'FALSE')}")
            
            async for event in story_runner.run_async(user_id=f"{user_id}_story", session_id=story_session.id, new_message=story_content):
                event_count += 1
                logger.info(f"📨 Received event #{event_count} from StoryAgent")
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            story_response += part.text
                            logger.info(f"📝 Accumulated {len(story_response)} characters so far")
                            
                            # Send partial updates to frontend every few events
                            if event_count % 3 == 0:
                                await websocket.send_text(json.dumps({
                                    "type": "story_chunk",
                                    "data": part.text,
                                    "partial": True
                                }))
                                
        except Exception as runner_error:
            logger.error(f"❌ StoryAgent execution error: {runner_error}")
            import traceback
            logger.error(f"📋 StoryAgent error traceback: {traceback.format_exc()}")
            raise runner_error
        
        logger.info(f"✅ StoryAgent completed after {event_count} events. Total response length: {len(story_response)}")
        
        # Parse the JSON response from StoryAgent
        try:
            logger.info("🔍 Parsing JSON response from StoryAgent...")
            # Clean the response - remove markdown code blocks if present
            cleaned_response = story_response.strip()
            logger.info(f"📄 Raw response length: {len(story_response)} characters")
            logger.info(f"📄 First 200 chars: {story_response[:200]}")
            
            if cleaned_response.startswith("```json"):
                cleaned_response = cleaned_response[7:]  # Remove ```json
                logger.info("🧹 Removed ```json prefix")
            if cleaned_response.endswith("```"):
                cleaned_response = cleaned_response[:-3]  # Remove ```
                logger.info("🧹 Removed ``` suffix")
            cleaned_response = cleaned_response.strip()
            
            logger.info(f"🧹 Cleaned response length: {len(cleaned_response)} characters")
            story_data = json.loads(cleaned_response)
            logger.info(f"✅ Story generated successfully with {len(story_data.get('scenes', []))} scenes")
            logger.info(f"📊 Story data keys: {list(story_data.keys()) if isinstance(story_data, dict) else 'Not a dict'}")
            
            # Reconstruct story text with scene markers for frontend compatibility
            scenes = story_data.get("scenes", [])
            if scenes:
                # Build story text with [SCENE X] markers that frontend expects
                story_with_markers = ""
                for scene in scenes:
                    scene_index = scene.get("index", 1)
                    scene_text = scene.get("text", "")
                    story_with_markers += f"[SCENE {scene_index}]\n{scene_text}\n\n"
                story_text = story_with_markers.strip()
            else:
                # Fallback to the raw story if no scenes
                story_text = story_data.get("story", "")
            
            # Send story in chunks to avoid message size limits
            chunk_size = 2000  # Send in 2KB chunks
            if len(story_text) > chunk_size:
                # Send in chunks
                for i in range(0, len(story_text), chunk_size):
                    chunk = story_text[i:i + chunk_size]
                    is_final = i + chunk_size >= len(story_text)
                    await websocket.send_text(json.dumps({
                        "type": "story_chunk", 
                        "data": chunk,
                        "partial": not is_final
                    }))
                    
                # Send completion marker
                await websocket.send_text(json.dumps({
                    "type": "story_complete", 
                    "data": ""  # Empty data since content was already sent in chunks
                }))
            else:
                # Send complete story if small enough
                await websocket.send_text(json.dumps({
                    "type": "story_complete", 
                    "data": story_text
                }))
            logger.info(f"📤 Sent story text with scene markers to frontend ({len(story_text)} characters)")
            
        except json.JSONDecodeError as e:
            logger.error(f"❌ Failed to parse story JSON: {e}")
            logger.error(f"📄 Raw response (first 500 chars): {story_response[:500]}")
            logger.error(f"📄 Cleaned response (first 500 chars): {cleaned_response[:500] if 'cleaned_response' in locals() else 'N/A'}")
            raise Exception(f"Story agent returned invalid JSON format: {e}")
            
    except Exception as e:
        logger.error(f"❌ Story generation failed for user {user_id}: {e}")
        import traceback
        logger.error(f"📋 Full story generation traceback: {traceback.format_exc()}")
        await websocket.send_text(json.dumps({"type": "error", "message": f"Story generation failed: {str(e)}", "data": str(e)}))
        return

    # Step 2: Generate images using DirectImageAgent
    if story_data and direct_image_agent and story_data.get("scenes"):
        logger.info("🎨 Starting image generation with DirectImageAgent...")
        
        # Extract character descriptions from story data
        character_descriptions = {}
        if story_data.get("main_characters"):
            for character in story_data["main_characters"]:
                char_name = character.get("name", "")
                char_desc = character.get("description", "")
                if char_name and char_desc:
                    character_descriptions[char_name] = char_desc
            logger.info(f"📚 Found {len(character_descriptions)} main character(s): {', '.join(character_descriptions.keys())}")
        
        for scene in story_data["scenes"]:
            scene_index = scene.get("index", 1) - 1  # Convert to 0-based index
            scene_description = scene.get("description", "")
            
            try:
                logger.info(f"🖼️ Generating image for scene {scene_index + 1}: {scene.get('title', 'Unknown')}")
                
                # Send a heartbeat to keep the WebSocket alive during heavy AI processing
                await websocket.send_text(json.dumps({
                    "type": "processing",
                    "message": f"Painting scene {scene_index + 1}..."
                }))

                # Use DirectImageAgent to generate image with character descriptions
                result_data = await direct_image_agent.generate_image_from_description(
                    scene_description,
                    character_descriptions
                )
                
                if result_data.get("success") and result_data.get("images"):
                    for img_data in result_data["images"]:
                        image_payload = {
                            "index": scene_index,
                            "scene_title": scene.get("title", ""),
                            "format": img_data.get("format", "png"),
                            "stored_in_bucket": img_data.get("stored_in_bucket", False)
                        }
                        
                        # Include GCS URL if available
                        if img_data.get("gcs_url"):
                            image_payload["gcs_url"] = img_data["gcs_url"]
                            logger.info(f"✅ Generated image for scene {scene_index + 1} with GCS URL")
                        
                        # Include base64 if available (for fallback)
                        if img_data.get("base64"):
                            image_payload["base64"] = img_data["base64"]
                        
                        await websocket.send_text(json.dumps({
                            "type": "image_generated",
                            "data": image_payload
                        }))
                        logger.info(f"📤 Sent image for scene {scene_index + 1} to frontend")
                else:
                    raise Exception(f"Image generation failed: {result_data.get('error', 'Unknown error')}")
                    
            except Exception as e:
                logger.error(f"Image generation failed for scene {scene_index + 1}: {e}")
                # Send error placeholder so frontend knows this slot exists
                error_payload = {
                    "index": scene_index,
                    "scene_title": scene.get("title", ""),
                    "format": "png",
                    "stored_in_bucket": False,
                    "error": f"Image generation failed: {str(e)}",
                    "placeholder": True
                }
                await websocket.send_text(json.dumps({
                    "type": "image_generated",
                    "data": error_payload
                }))
                logger.info(f"📤 Sent error placeholder for scene {scene_index + 1}")
            
            # Small delay between images to avoid rate limiting
            if scene_index < len(story_data["scenes"]) - 1:
                await asyncio.sleep(2)
                
        logger.info("🎨 All image generation completed")
    else:
        if not direct_image_agent:
            logger.warning("⚠️ DirectImageAgent not available, skipping image generation")
        elif not story_data.get("scenes"):
            logger.warning("⚠️ No scenes found in story data, skipping image generation")
    
    # Send completion notification
    await websocket.send_text(json.dumps({"type": "turn_complete", "turn_complete": True}))



@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    """
    WebSocket endpoint for real-time story generation
    
    Args:
        websocket: WebSocket connection
        user_id: Unique user identifier
    """
    await websocket.accept()
    logger.info(f"Client #{user_id} connected")

    try:
        # Send connection confirmation
        await websocket.send_text(json.dumps({
            "type": "connected",
            "message": "Connected to StoryGen backend"
        }))

        while True:
            # Receive message from client
            logger.info(f"⏳ Waiting for message from user {user_id}")
            message_json = await websocket.receive_text()
            logger.info(f"📩 Received raw message from user {user_id}: {message_json}")
            message = json.loads(message_json)
            
            message_type = message.get("type")
            data = message.get("data", "")
            
            if message_type == "generate_story":
                logger.info(f"🎯 Story generation request received from user {user_id}: '{data}'")
                try:
                    # Send processing notification
                    await websocket.send_text(json.dumps({
                        "type": "processing",
                        "message": "Generating story and images..."
                    }))
                    logger.info(f"📤 Sent processing notification to user {user_id}")
                    
                    # Run the clean two-agent workflow
                    logger.info(f"🚀 Starting two-agent workflow for user {user_id}")
                    await run_two_agent_workflow(websocket, user_id, data)
                    logger.info(f"✅ Completed two-agent workflow for user {user_id}")
                    
                except Exception as e:
                    logger.error(f"❌ Error in websocket workflow for user {user_id}: {e}")
                    import traceback
                    logger.error(f"📋 Full traceback: {traceback.format_exc()}")
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"Story generation failed: {str(e)}"
                    }))
                
            elif message_type == "ping":
                # Handle ping/keepalive messages
                await websocket.send_text(json.dumps({"type": "pong"}))
                
            else:
                logger.warning(f"Unknown message type: {message_type}")

    except WebSocketDisconnect:
        logger.info(f"Client #{user_id} disconnected")
    except Exception as e:
        logger.error(f"WebSocket error for user {user_id}: {e}")
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": f"Server error: {str(e)}"
            }))
        except:
            pass
    finally:
        logger.info(f"Client #{user_id} connection closed")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "storygen-backend"}

@app.get("/")
async def root():
    """Root endpoint"""
    return {"message": "StoryGen Backend API", "version": "2.0.0", "workflow": "sequential"}

if __name__ == "__main__":
    import uvicorn
    # Cloud Run provides the PORT environment variable
    port = int(os.environ.get("PORT", 8080))
    
    logger.info("========================================")
    logger.info(f"🚀 StoryGen Backend active on port: {port}")
    logger.info(f"🌍 Binding to host 0.0.0.0")
    logger.info("========================================")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")