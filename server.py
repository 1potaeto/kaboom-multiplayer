import os
import random
import simplejson as json
import eventlet
from flask import Flask, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room, close_room

# --- Configuration ---
# Use eventlet as the WSGI server for WebSocket compatibility
eventlet.monkey_patch() 

app = Flask(__name__)
# WARNING: In production, SECRET_KEY should be set and managed securely
app.config['SECRET_KEY'] = 'a-super-secret-key-for-kaboom' 
# Use the PORT environment variable provided by Render, default to 5000
PORT = int(os.environ.get('PORT', 5000))
# Configure SocketIO to use the eventlet message queue
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- Game Logic Classes ---

class Card:
    """Represents a single playing card."""
    def __init__(self, rank, suit=None):
        self.rank = rank
        self.suit = suit
        self.is_face_up = False 

    def to_dict(self):
        """Converts Card object to a serializable dictionary."""
        return {
            'rank': self.rank,
            'suit': self.suit,
            'is_face_up': self.is_face_up,
            'display_name': self.get_display_name()
        }

    def get_display_name(self):
        """Returns the short name (e.g., 'AH', 'Joker')."""
        if self.rank == 'Joker':
            return "Joker"
        return f"{self.rank}{self.suit[0]}"

    def get_score_value(self):
        """Calculates the card's value for final hand scoring."""
        if self.rank == 'Joker': return -1
        if self.rank in ['K', 'Q', 'J']: return 10
        if self.rank == 'A': return 1
        try:
            return int(self.rank)
        except ValueError:
            return 0 # Should not happen

class Deck:
    """Manages the deck and discard pile."""
    RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
    SUITS = ['Hearts', 'Diamonds', 'Clubs', 'Spades']

    def __init__(self):
        self.cards = []
        self._create_deck()
        random.shuffle(self.cards)
        self.discard_pile = []

    def _create_deck(self):
        """Initializes the 54-card deck."""
        for suit in self.SUITS:
            for rank in self.RANKS:
                self.cards.append(Card(rank, suit))
        self.cards.append(Card('Joker'))
        self.cards.append(Card('Joker'))

    def draw_card(self):
        """Draws and returns the top card, recycling discard if empty."""
        if not self.cards:
            if not self.discard_pile:
                return None
            # Recycle discard pile
            self.cards = self.discard_pile
            self.discard_pile = []
            random.shuffle(self.cards)
            # Notify clients of reshuffle (optional)
            
        return self.cards.pop()

# --- Game State Management ---

class GameState:
    """The central state for the game."""
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = {}  # {sid: {'name': name, 'hand': [Card objects]}}
        self.deck = Deck()
        self.drawn_card = None
        self.turn_order = []  # List of sids
        self.current_turn_index = 0
        self.status = "WAITING"  # WAITING, SETUP, PLAYING, KABOOM, ENDED
        self.max_players = 2

    @property
    def is_full(self):
        return len(self.players) >= self.max_players

    def get_player_sid(self):
        """Returns the SID of the current player."""
        if not self.turn_order: return None
        return self.turn_order[self.current_turn_index]

    def advance_turn(self):
        """Moves to the next player."""
        self.current_turn_index = (self.current_turn_index + 1) % len(self.turn_order)

    def get_state_for_client(self, sid):
        """
        Prepares the full game state to be sent to a specific client.
        Crucially, only the client's own hand cards are fully visible.
        """
        turn_sid = self.get_player_sid()
        
        # Prepare player data (hands must be masked for opponents)
        players_data = []
        for player_sid, data in self.players.items():
            hand_data = []
            for card in data['hand']:
                card_dict = card.to_dict()
                # Crucial Masking Logic:
                # 1. If it's your own card, you see it (even if face down, you know what it is).
                # 2. If it's face up (due to an action), everyone sees it.
                if player_sid != sid and not card.is_face_up:
                    card_dict['display_name'] = '??' # Hide rank/suit for others
                    card_dict['rank'] = 'HIDDEN' # Mask rank
                    card_dict['suit'] = 'HIDDEN' # Mask suit
                
                hand_data.append(card_dict)

            players_data.append({
                'sid': player_sid,
                'name': data['name'],
                'hand': hand_data,
                'is_current_player': (player_sid == turn_sid)
            })

        return {
            'room_id': self.room_id,
            'status': self.status,
            'players': players_data,
            'deck_count': len(self.deck.cards),
            'discard_count': len(self.deck.discard_pile),
            'drawn_card': self.drawn_card.to_dict() if self.drawn_card else None,
            'current_turn_sid': turn_sid
        }

    def start_game(self):
        """Deals cards and moves to the PLAYING status."""
        self.status = "PLAYING"
        self.turn_order = list(self.players.keys())
        
        for player_sid, data in self.players.items():
            for i in range(4):
                card = self.deck.draw_card()
                if card:
                    # Initial "look" phase: Mark the first two cards face up
                    if i < 2:
                        card.is_face_up = True
                    data['hand'].append(card)

        # Randomly choose who goes first
        random.shuffle(self.turn_order)
        self.current_turn_index = 0

# Global storage for active games
active_games = {} # {room_id: GameState object}

# --- Utility Functions ---

def broadcast_game_state(room_id):
    """Sends the current, masked game state to all players in the room."""
    game = active_games.get(room_id)
    if not game: return

    for sid in game.turn_order:
        state = game.get_state_for_client(sid)
        # Use simplejson for serialization to handle complex object structures better
        socketio.emit('game_state_update', json.dumps(state), room=sid)

def get_game_and_player(sid):
    """Finds the game and player associated with a session ID."""
    for room_id, game in active_games.items():
        if sid in game.players:
            return game, game.players[sid]
    return None, None

# --- Flask Routes ---

@app.route('/')
def index():
    """Serves the main client HTML page."""
    # Since all the client code is in one index.html, we just serve that file.
    return send_from_directory('.', 'index.html')

# --- SocketIO Handlers ---

@socketio.on('join_game')
def handle_join_game(data):
    """Handles a player connecting and joining/creating a room."""
    sid = random.choice(list(active_games.keys())) if not active_games else 'game-' + str(random.randint(100, 999))
    
    # Simple logic: Try to join an existing game, otherwise start a new one
    room_id = next((rid for rid, game in active_games.items() if not game.is_full), sid)
    
    if room_id not in active_games:
        # Create new game
        active_games[room_id] = GameState(room_id)
        
    game = active_games[room_id]
    
    if game.is_full and game.status != "PLAYING":
        # Should not happen with the logic above, but handle overflow
        emit('error', 'Room is full. Try again later.')
        return
    
    # Add player to game state and room
    player_name = data.get('name', f"Guest-{len(game.players) + 1}")
    game.players[sid] = {'name': player_name, 'hand': []}
    join_room(sid) # Join the room associated with their session ID (for private messages)
    join_room(room_id) # Join the main game room
    
    print(f"Player {player_name} joined room {room_id}. Total players: {len(game.players)}")
    
    if game.is_full:
        game.start_game()
        socketio.emit('game_message', f"Game starting! {game.players[game.turn_order[0]]['name']} goes first.", room=room_id)
        
    broadcast_game_state(room_id)

@socketio.on('draw_card')
def handle_draw_card():
    """Handles a player drawing a card from the deck."""
    game, player_data = get_game_and_player(request.sid)
    if not game or game.status != "PLAYING" or game.get_player_sid() != request.sid:
        emit('error', 'It is not your turn, or the game is not ready.')
        return

    if game.drawn_card:
        emit('error', 'You already have a drawn card to resolve.')
        return

    drawn_card = game.deck.draw_card()
    if not drawn_card:
        socketio.emit('game_message', "Deck is empty! Game might be nearing its end.", room=game.room_id)
        return
        
    # Card is drawn, reveal it and set it as the drawn card
    drawn_card.is_face_up = True
    game.drawn_card = drawn_card
    
    socketio.emit('game_message', f"{player_data['name']} drew a {drawn_card.get_display_name()}. Action required.", room=game.room_id)
    broadcast_game_state(game.room_id)

@socketio.on('replace_card')
def handle_replace_card(data):
    """Handles a player replacing a hand card with the drawn card."""
    hand_pos = data.get('position')
    
    game, player_data = get_game_and_player(request.sid)
    if not game or game.status != "PLAYING" or game.get_player_sid() != request.sid:
        emit('error', 'It is not your turn, or the game is not ready.')
        return

    if not game.drawn_card:
        emit('error', 'No card has been drawn.')
        return

    if hand_pos is None or not 0 <= hand_pos < 4:
        emit('error', 'Invalid hand position specified (must be 0-3).')
        return

    # 1. Perform swap
    old_card = player_data['hand'][hand_pos]
    player_data['hand'][hand_pos] = game.drawn_card
    
    # 2. Reveal the new card in hand
    player_data['hand'][hand_pos].is_face_up = True
    
    # 3. Discard the old card
    game.deck.discard_pile.append(old_card)
    game.drawn_card = None

    # 4. Advance turn and broadcast
    game.advance_turn()
    socketio.emit('game_message', f"{player_data['name']} replaced a card at position {hand_pos}. Turn over.", room=game.room_id)
    broadcast_game_state(game.room_id)

@socketio.on('discard_drawn_card')
def handle_discard():
    """Handles a player discarding the drawn card."""
    game, player_data = get_game_and_player(request.sid)
    if not game or game.status != "PLAYING" or game.get_player_sid() != request.sid:
        emit('error', 'It is not your turn, or the game is not ready.')
        return

    if not game.drawn_card:
        emit('error', 'No card has been drawn to discard.')
        return

    # 1. Discard
    game.deck.discard_pile.append(game.drawn_card)
    game.drawn_card = None

    # 2. Advance turn and broadcast
    game.advance_turn()
    socketio.emit('game_message', f"{player_data['name']} discarded the drawn card. Turn over.", room=game.room_id)
    broadcast_game_state(game.room_id)

@socketio.on('disconnect')
def handle_disconnect():
    """Cleans up when a player leaves."""
    game, player_data = get_game_and_player(request.sid)
    if game:
        del game.players[request.sid]
        game.turn_order = [sid for sid in game.turn_order if sid != request.sid]
        
        socketio.emit('game_message', f"Player {player_data['name']} disconnected. Waiting for players.", room=game.room_id)
        game.status = "WAITING"
        if not game.players:
            del active_games[game.room_id]
            print(f"Room {game.room_id} closed.")
        else:
            broadcast_game_state(game.room_id)
    
    print(f"Client disconnected: {request.sid}")


# --- Main Execution ---

if __name__ == '__main__':
    # When running locally, use Flask's default port 5000
    print(f"Starting server on port {PORT}...")
    # Render will execute this using eventlet and the environment PORT
    socketio.run(app, host='0.0.0.0', port=PORT) 

