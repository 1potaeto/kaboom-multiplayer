import simplejson
import eventlet
from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO

# Suppress eventlet warnings related to monkey patching
eventlet.monkey_patch()

# Initialize Flask App
app = Flask(__name__)
# IMPORTANT: Use a real secret key in production, but this placeholder is fine for now
app.config['SECRET_KEY'] = 'your_secret_key' 
# Initialize SocketIO, allowing connections from any domain (crucial for multiplayer)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Dictionary to hold the real-time position and state of all players
player_states = {}

# --- Routes ---

# Serves the index.html file for the main game page
# Render will look for this route as the entry point
@app.route('/')
def index():
    # Looks for index.html in the same directory as server.py
    return send_from_directory('.', 'index.html')

# --- SocketIO Events ---

@socketio.on('connect')
def handle_connect():
    """Handles a new client connecting to the server."""
    player_id = request.sid
    print(f'Client {player_id} connected.')

    # 1. Send the new player their unique ID
    socketio.emit('your_id', player_id, room=player_id)
    
    # 2. Send the existing game state to the new player
    # We use simplejson.dumps() to ensure complex data is transmitted correctly
    socketio.emit('initial_state', simplejson.dumps(player_states), room=player_id)
    
    # 3. Broadcast the new player's connection to everyone else
    socketio.emit('player_connected', player_id, broadcast=True, include_self=False)
    
    # Initialize the new player's starting state
    player_states[player_id] = {'x': 100, 'y': 100, 'dir': 'down'}


@socketio.on('disconnect')
def handle_disconnect():
    """Handles a client disconnecting from the server."""
    player_id = request.sid
    if player_id in player_states:
        del player_states[player_id]
        print(f'Client {player_id} disconnected.')
        # Broadcast removal to all remaining clients
        socketio.emit('player_disconnected', player_id, broadcast=True)

@socketio.on('player_data')
def handle_player_data(data):
    """Handles frequent position updates from a client."""
    player_id = request.sid
    
    try:
        data_obj = simplejson.loads(data)
    except Exception as e:
        print(f"Error parsing JSON data from {player_id}: {e}")
        return

    # Update the server's internal game state
    if player_id in player_states:
        # Simple safety check to only update expected keys
        player_states[player_id]['x'] = data_obj.get('x', player_states[player_id]['x'])
        player_states[player_id]['y'] = data_obj.get('y', player_states[player_id]['y'])
        player_states[player_id]['dir'] = data_obj.get('dir', player_states[player_id]['dir'])
        
    # Broadcast the data to all other players (they use this to update their screen)
    socketio.emit('player_moved', simplejson.dumps({
        'id': player_id, 
        'data': data_obj
    }), broadcast=True, include_self=False)

# CRITICAL: We expose the 'app' object here. Gunicorn will look for 'server:app'
# to start the application using the Start Command: gunicorn --worker-class eventlet -w 1 server:app
