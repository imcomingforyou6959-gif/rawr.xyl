import asyncio
import websockets
import json
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('WebSocketServer')

# Store connected clients
connected_clients = set()
message_history = []  # Store last 100 messages

async def broadcast(message):
    """Send message to all connected clients"""
    if connected_clients:
        await asyncio.gather(
            *[client.send(json.dumps(message)) for client in connected_clients],
            return_exceptions=True
        )

async def handler(websocket):
    """Handle WebSocket connections"""
    connected_clients.add(websocket)
    logger.info(f"Client connected. Total: {len(connected_clients)}")
    
    # Send last 50 messages to new client
    if message_history:
        for msg in message_history[-50:]:
            await websocket.send(json.dumps(msg))
    
    try:
        async for message in websocket:
            data = json.loads(message)
            
            # Add timestamp
            data['timestamp'] = datetime.utcnow().isoformat()
            data['id'] = f"msg_{int(datetime.utcnow().timestamp() * 1000)}"
            
            # Store in history
            message_history.append(data)
            if len(message_history) > 100:
                message_history.pop(0)
            
            # Broadcast to all clients
            await broadcast(data)
            logger.info(f"Message broadcasted: {data.get('type', 'unknown')}")
            
    except websockets.exceptions.ConnectionClosed:
        logger.info("Client disconnected")
    finally:
        connected_clients.remove(websocket)

async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765):
        logger.info("WebSocket server started on port 8765")
        await asyncio.Future()  # Run forever

if __name__ == "__main__":
    asyncio.run(main())
