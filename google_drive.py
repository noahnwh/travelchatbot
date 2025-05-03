import io
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2.service_account import Credentials
import pandas as pd
import logging

SERVICE_ACCOUNT_FILE = 'config/service_account.json'  # Update path
SCOPES = ['https://www.googleapis.com/auth/drive.file']

# Authenticate using the Service Account
def authenticate_google_drive():
    credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    drive_service = build('drive', 'v3', credentials=credentials)
    return drive_service

# Upload file to Google Drive directly from memory
def upload_file_to_drive(file_stream, mime_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', folder_id=None):
    """Uploads or appends a file to Google Drive from an in-memory stream."""
    logging.debug("Starting the upload to Google Drive...")

    drive_service = authenticate_google_drive()

    # File metadata (file name and parent folder if provided)
    file_metadata = {'name': 'chat_history.xlsx' if mime_type == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' else 'Error_Log.txt'}
    if folder_id:
        file_metadata['parents'] = [folder_id]  # Ensure file goes into the correct folder

    try:
        logging.debug(f"Checking if file {file_metadata['name']} exists in folder {folder_id}...")
        
        # Check if the file already exists in the folder
        results = drive_service.files().list(q=f"name = '{file_metadata['name']}' and '{folder_id}' in parents",
                                             fields="files(id, name)").execute()
        items = results.get('files', [])

        if items:
            # If the file exists, download the existing file
            file_id = items[0]['id']
            logging.debug(f"File exists, file ID: {file_id}")
            
            if mime_type == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':
                # If it's an Excel file, download and append the data
                request = drive_service.files().get_media(fileId=file_id)
                existing_file = io.BytesIO(request.execute())

                # Read the existing Excel file into a DataFrame
                existing_df = pd.read_excel(existing_file)
                logging.debug(f"Existing DataFrame:\n{existing_df}")

                # Append the new data to the existing DataFrame
                new_data = pd.read_excel(file_stream)
                updated_df = pd.concat([existing_df, new_data], ignore_index=True)

                # Save the updated DataFrame back to the file stream
                updated_file_stream = io.BytesIO()
                with pd.ExcelWriter(updated_file_stream, engine='xlsxwriter') as writer:
                    updated_df.to_excel(writer, index=False, sheet_name="Conversation")
                updated_file_stream.seek(0)  # Rewind the file for upload

                # Re-upload the updated file
                media = MediaIoBaseUpload(updated_file_stream, mimetype=mime_type)
                drive_service.files().update(fileId=file_id, media_body=media).execute()
                logging.debug(f"File updated successfully! File ID: {file_id}")
            elif mime_type == 'text/plain':
                # If it's a text file, append the new log entry to the existing file
                request = drive_service.files().get_media(fileId=file_id)
                existing_file = io.BytesIO(request.execute())

                # Read the existing text file (error log) into memory
                existing_log = existing_file.getvalue().decode('utf-8')

                # Append the new error log entry
                new_log_entry = file_stream.getvalue().decode('utf-8')
                updated_log = existing_log + new_log_entry

                # Re-upload the updated error log file
                updated_file_stream = io.BytesIO(updated_log.encode('utf-8'))
                media = MediaIoBaseUpload(updated_file_stream, mimetype=mime_type)
                drive_service.files().update(fileId=file_id, media_body=media).execute()
                logging.debug(f"Error log updated successfully! File ID: {file_id}")
        else:
            # If the file doesn't exist, create a new file
            logging.debug("File does not exist. Creating a new file...")
            file_stream.seek(0)  # Rewind the stream
            media = MediaIoBaseUpload(file_stream, mimetype=mime_type)
            file = drive_service.files().create(media_body=media, body=file_metadata, fields='id').execute()
            logging.debug(f"File created successfully! File ID: {file['id']}")
            return file['id']  # Return the new file ID

    except Exception as e:
        logging.error(f"Error while checking/updating file: {str(e)}")
        return None  # Return None in case of an error
