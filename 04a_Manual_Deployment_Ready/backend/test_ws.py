import asyncio
import websockets
import json
import os
import subprocess

async def test_story():
    # Dynamically get the latest URL from gcloud
    print("🔍 Fetching service URL...")
    cmd = "gcloud run services describe genai-backend --region=us-central1 --format='value(status.url)'"
    url = subprocess.check_output(cmd, shell=True).decode().strip()
    
    host = url.replace("https://", "")
    uri = f"wss://{host}/ws/test-user"
    
    print(f"🔌 Connecting to {uri}...")
    try:
        async with websockets.connect(uri) as websocket:
            print("✅ Connected!")
            
            # 1. Initial Connection Message
            init_resp = await websocket.recv()
            print(f"📥 Server: {init_resp}")
            
            # 2. Send Request
            payload = {
                "type": "generate_story",
                "data": "A futuristic robot discovering a flower in a wasteland"
            }
            print(f"📤 Sending: {payload['data']}")
            await websocket.send(json.dumps(payload))
            
            # 3. Listen for streaming events
            while True:
                response = await websocket.recv()
                data = json.loads(response)
                print(f"📦 Event: {data.get('type')} | Content Length: {len(str(data.get('data', '')))}")
                
                if data.get("type") == "error":
                    print(f"❌ Server Error: {data.get('message') or data.get('error') or data}")
                    break

                if data.get("type") == "turn_complete":
                    print("✨ Story and images generated successfully!")
                    break
    except Exception as e:
        print(f"❌ Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_story())