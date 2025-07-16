import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client, Client
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
# Adjust CORS origin for production
CORS(app, resources={r"/api/*": {"origins": "http://localhost:4200"}})

# Initialize Supabase Admin client
try:
    SUPABASE_URL: str = os.environ.get("SUPABASE_URL")
    SUPABASE_SERVICE_ROLE_KEY: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")

    supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    supabase_admin_auth = supabase.auth.admin
    print("Supabase admin client initialized successfully.")
except Exception as e:
    print(f"Error initializing Supabase client: {e}")
    supabase_admin_auth = None # Set to None if initialization fails


@app.route('/api/create-supabase-user', methods=['POST'])
def create_supabase_user():
    if not supabase_admin_auth:
        return jsonify({'error': 'Backend setup issue: Supabase client not initialized'}), 500

    data = request.get_json()
    nickname = data.get('nickname')
    phone_number = data.get('phoneNumber')
    password = data.get('password')

    if not nickname or not phone_number or not password:
        return jsonify({'error': 'Nickname, phone number, and password are required.'}), 400

    try:
        user_response = supabase_admin_auth.create_user(
            {
                "phone": phone_number,
                "password": password,
                "phone_confirm": True
            }
        )

        if user_response.user is None:
            # Check for specific error message from Supabase
            error_message_detail = user_response.dict().get('msg', 'Unknown error during user creation.')
            print(f"Supabase create_user raw response: {user_response}") # Log for debugging
            
            if 'User already exists' in error_message_detail:
                # Return 409 Conflict for duplicate user
                return jsonify({'error': 'This phone number is already registered. Please sign in.'}), 409
            
            return jsonify({'error': f'Failed to create user: {error_message_detail}'}), 500

        user_id = user_response.user.id
        print(f"User created in auth.users: {user_id}")

        profile_response = supabase.table("profiles").insert(
            {
                "id": user_id,
                "nickname": nickname,
                "phone_number": phone_number
            }
        ).execute()

        if profile_response.data is None or len(profile_response.data) == 0:
            print(f"Supabase profile insertion error: {profile_response}")
            supabase_admin_auth.delete_user(user_id) # Cleanup user if profile fails
            return jsonify({'error': 'Failed to create user profile.'}), 500

        print(f"Profile created for user: {user_id}")
        return jsonify({'message': 'Account created successfully!', 'userId': user_id}), 200

    except Exception as e:
        # Catch any other unexpected exceptions
        print(f"An unexpected error occurred in backend: {e}")
        # Note: You might inspect 'e' for more specific messages,
        # but for a general catch-all, keep it generic for the client.
        return jsonify({'error': 'An unexpected error occurred on the server.'}), 500


if __name__ == '__main__':
    PORT = os.environ.get("PORT", 5000) # Ensure Flask runs on port 5000 as per your log
    app.run(debug=True, port=PORT)