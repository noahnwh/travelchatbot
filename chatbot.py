from gevent import monkey
monkey.patch_all()
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from flask_socketio import SocketIO #Added for socket io
from datetime import datetime, timedelta, timezone
import openai
import os
import pandas as pd

import io
import logging
from google_drive import upload_file_to_drive 

# google_drive folder id
folder_id = "1IuXLDLPvNA8edE2CG0kR9J7UHy-yt3Tp"

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": [
    "http://localhost:5000",
    "http://127.0.0.1:5000",
    "https://mobile-travel-chatbot.onrender.com"
]}})

socketio = SocketIO(app, cors_allowed_origins="*")

# API Key stored in environment variable
key = os.getenv("OPENAI_API_KEY")

connected_users = {} # { client_id: { sid: ..., needs_help: ..., chat_log: [...] } }
chat_logs = {} 


def load_text_file(filepath):
    with open(filepath, "r", encoding="utf-8") as file:
        return file.read()
    
# info = load_text_file("prompts/ ")
base_role = load_text_file("prompts/base_role.txt")
greeting_reply = load_text_file("prompts/greeting_reply.txt")
out_of_scope_reply = load_text_file("prompts/out_of_scope_reply.txt")



initial_context = [
    {"role": "system", "content": base_role},
    {"role": "system", "content": greeting_reply},
    {"role": "system", "content": out_of_scope_reply}
]

def get_malaysia_time():
    utc_now = datetime.now(timezone.utc)
    malaysia_time = utc_now + timedelta(hours=8)  # Malaysia/Singapore = UTC+8
    return malaysia_time.strftime('%Y-%m-%d'), malaysia_time.strftime('%H:%M:%S')


def save_chat(user_msg, chatbot_response, client_id):
    # date = datetime.now().strftime('%Y-%m-%d')
    # time = datetime.now().strftime('%H:%M:%S')
    date, time = get_malaysia_time()
    logging.debug(f"Saving chat... Client ID: {client_id}, User Msg: {user_msg}, Chatbot Msg: {chatbot_response}")
    entry = {"Date":[date], "Time":[time], "Client ID": [client_id], "Client Message":[user_msg], "Chatbot Message":[chatbot_response]}
    df = pd.DataFrame(entry)

    logging.debug(f"DataFrame to upload:\n{df}")
    # Convert the DataFrame to an in-memory Excel file (file stream)
    try:
        file_stream = io.BytesIO()
        with pd.ExcelWriter(file_stream, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name="Conversation")
            # No need for writer.save() here, the context manager handles it
        
        # Check if file stream is empty after writing
        file_stream.seek(0)  # Rewind the file for upload
        if len(file_stream.getvalue()) == 0:
            logging.error("File stream is empty! Upload will not proceed.")
            return None  # Prevent upload if file is empty

        logging.debug(f"File stream created successfully. Length: {len(file_stream.getvalue())}")
        mime_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        return file_stream, mime_type
    
    except Exception as e:
        logging.error(f"Error creating file stream: {str(e)}")
        return None  # Return None if there is any error in creating the file


def background_upload(file_stream, mime_type, folder_id):
    logging.debug("Starting background upload task...")

    try:
        # Upload to Google Drive in the background
        upload_file_to_drive(file_stream, mime_type=mime_type, folder_id=folder_id)
        logging.debug("Upload completed successfully in the background.")
    except Exception as e:
        logging.error(f"Error uploading to Google Drive in background: {str(e)}")


def save_errorLog(error_msg):
    filename = "Error_Log.txt"
    # Saves the error log and uploads it to Google Drive

    date, time = get_malaysia_time()
    # Prepare the log entry
    log_entry = f"[{date} {time}] - {error_msg}\n"

    # Create an in-memory file (BytesIO stream) to store the error log
    try:
        # Initialize an in-memory file (file stream) using BytesIO
        file_stream = io.BytesIO()

        # Write the log entry to the in-memory file (file_stream)
        file_stream.write(log_entry.encode('utf-8'))  # Write the log as bytes
        
        # Log the file stream length for debugging purposes
        file_stream.seek(0)  # Rewind the stream before using it
        logging.debug(f"File stream for error log created with length: {len(file_stream.getvalue())}")

        # Check if the file stream has data (it's not empty)
        if len(file_stream.getvalue()) == 0:
            logging.error("File stream is empty! Upload will not proceed.")
            return None  # Return None if the file stream is empty
        
        # Return the file stream and specify the MIME type for a text file
        mime_type = 'text/plain'  # MIME type for text files
        # Return the file stream for upload to Google Drive
        return file_stream, mime_type

    except Exception as e:
        logging.error(f"Error while handling the error log in memory: {str(e)}")
        return None  # Return None if there’s an issue creating the file stream


@socketio.on('connect')
def connect():
    sid = request.sid
    referer = request.headers.get("Referer", "")
    logging.info(f"[CLIENT CONNECTED] SID: {sid}")
    # connected_users[sid] = {'takeover': False}
    socketio.emit('assign_sid', {'sid': sid}, to=sid)
    
@socketio.on('disconnect')
def disconnect():
    sid = request.sid
    client_to_remove = None
    for client_id, info in connected_users.items():
        if info.get('sid') == sid:
            client_to_remove = client_id
            break
    if client_to_remove:
        del connected_users[client_to_remove]
    print(f"A client disconnected with SID:{sid}, client_id: {client_to_remove}")
    
@socketio.on('get_connected_users')
def get_connected_users():
    sid = request.sid
    # Send only the necessary info
    simplified = {client_id: {
        'taken_over': info.get('takeover', False),
        'needs_help': info.get('needs_help', False)
    } for client_id, info in connected_users.items()}

    socketio.emit('connected_users_list', {'users': simplified}, to=sid)

@socketio.on('client_id')
def register_client_id(data):
    sid = request.sid
    client_id = data.get('client_id')

    if client_id not in connected_users:
        connected_users[client_id] = {
            'sid': sid,
            'needs_help': False,
            'chat_log': []
        }
    else:
        connected_users[client_id]['sid'] = sid  # update if reconnected


@app.route('/')
def home():
    return render_template("index.html")

@app.route('/ask', methods=['POST'])
def ask():
    try:
        global messages
        data = request.json
        user_message = data.get('message', '')
        user_sid = data.get('sid', None)
        client_id = data.get('client_id', None)
        if not client_id:
            return jsonify({'error': 'Missing client ID'}), 400

        # Register client_id if not yet tracked
        if client_id not in connected_users:
            connected_users[client_id] = {
                'sid': user_sid,
                'needs_help': False,
                'chat_log': []
            }

        # Update sid on each request in case user reconnects
        connected_users[client_id]['sid'] = user_sid

        max_history = 10  # last 10 exchanges
        conversation_history = chat_logs.get(client_id, [])[-max_history:]
        messages = initial_context + conversation_history + [{"role": "user", "content": user_message}]

        client = openai.OpenAI(api_key=key)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo-0125",
            messages=messages
        )

        # Get the response from OpenAI
        answer = response.choices[0].message.content

        # Save message to chat_logs
        chat_logs.setdefault(client_id, []).append({'role': 'user', 'content': user_message})
        chat_logs[client_id].append({'role': 'assistant', 'content': answer})
        
        
        global folder_id
        file_stream, mime_type = save_chat(user_message, answer, client_id)
        logging.debug(f"File stream created with length: {len(file_stream.getvalue())}")
        socketio.start_background_task(background_upload, file_stream, mime_type, folder_id)
        # Send the response back to the user
        return jsonify({'reply': answer})

    except Exception as e:
        error_message = str(e)

        file_stream, mime_type = save_errorLog(error_message)  # Save and return file stream
        if file_stream:
            logging.debug(f"File stream for error log created with length: {len(file_stream.getvalue())}")
            socketio.start_background_task(background_upload, file_stream, mime_type, folder_id)

        return jsonify({'error': 'Internal Server Error', 'message': str(e)}), 500



if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000)

