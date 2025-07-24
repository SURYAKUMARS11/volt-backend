# app.py

import datetime
import os
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash
from flask_cors import CORS
from supabase import create_client, Client
from dotenv import load_dotenv
import razorpay
import hmac
import hashlib
import uuid
from werkzeug.security import check_password_hash, generate_password_hash
from postgrest.exceptions import APIError
import logging
from functools import wraps # Ensure this is imported for @wraps
import uuid # 
# Configure basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app_logger = logging.getLogger(__name__)

# Load environment variables from .env file FIRST
load_dotenv()

app = Flask(__name__)
CORS(app)

# --- REQUIRED FOR ADMIN SESSION AUTHENTICATION ---
# IMPORTANT: Change this to a strong, random value and store it in an environment variable!
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "your_highly_secret_and_random_key_here_please_change_this")

# Admin credentials (for demo purposes ONLY - use environment variables or a proper DB in production!)
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin_user")
# Generate hash only once at startup
ADMIN_PASSWORD_HASH = generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin_pass"))

# Initialize Supabase Admin client
supabase: Client = None
supabase_admin_auth = None
try:
    SUPABASE_URL: str = os.environ.get("SUPABASE_URL")
    SUPABASE_SERVICE_ROLE_KEY: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")

    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    supabase_admin_auth = supabase.auth.admin
    app_logger.info("Supabase admin client initialized successfully.")
except Exception as e:
    app_logger.error(f"Error initializing Supabase client: {e}")
    supabase = None
    supabase_admin_auth = None

## Initialize Razorpay Client (for payments ONLY)
razorpay_client = None
try:
    RAZORPAY_KEY_ID: str = os.environ.get("RAZORPAY_KEY_ID")
    RAZORPAY_KEY_SECRET: str = os.environ.get("RAZORPAY_KEY_SECRET")

    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise ValueError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set in .env")

    razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    # REMOVE OR COMMENT OUT THIS LINE:
    # razorpay_client.set_app_details(app_name="VoltEarning", app_version="1.0")
    app_logger.info("Razorpay client (for payments) initialized successfully.")
except Exception as e:
    app_logger.error(f"Error initializing Razorpay client: {e}")
    razorpay_client = None

REFERRAL_QUESTS_CONFIG = [
    {"target": 5, "reward": 250},
    {"target": 10, "reward": 500},
    {"target": 20, "reward": 1500},
    {"target": 30, "reward": 3000},
    {"target": 50, "reward": 5000},
    {"target": 100, "reward": 10000}
]   
FRONTEND_SIGNUP_BASE_URL = os.environ.get("FRONTEND_SIGNUP_BASE_URL", "http://localhost:4200/signup")

# --- DECORATOR for Admin Authentication ---
def admin_required(f):
    @wraps(f) # This preserves original function's metadata
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function


# --- CORE LOGIC FUNCTION: update_withdrawal_status_logic ---
# This function contains the actual logic for updating transaction status
# and handling refunds. It is called by both the external API route and the internal admin route.
def update_withdrawal_status_logic(data):
    try:
        transaction_id = data.get('transaction_id')
        new_status = data.get('status')
        admin_notes = data.get('admin_notes', '')

        if not all([transaction_id, new_status]):
            return jsonify({'success': False, 'message': 'Transaction ID and new status are required.'}), 400

        if new_status not in ['completed', 'rejected', 'failed']:
            return jsonify({'success': False, 'message': 'Invalid status. Allowed: completed, rejected, failed.'}), 400

        update_payload = {
            'status': new_status,
            'admin_notes': admin_notes,
            'updated_at': datetime.datetime.now().isoformat()
        }

        # Handle refund if the status is rejected or failed
        if new_status in ['rejected', 'failed']:
            if not supabase:
                app_logger.error("Supabase client not initialized, cannot process refund for rejected/failed withdrawal.")
                return jsonify({'success': False, 'message': 'Backend error: Database connection issue for refund.'}), 500

            # Fetch the original transaction to get user_id and amount
            transaction_response = supabase.table('transactions') \
                                   .select('user_id, amount') \
                                   .eq('id', transaction_id) \
                                   .single() \
                                   .execute()

            if transaction_response.data:
                user_id = transaction_response.data['user_id']
                amount_to_refund = transaction_response.data['amount']

                # Fetch and update the user's wallet balance
                wallet_response = supabase.table('user_wallets') \
                                  .select('balance') \
                                  .eq('user_id', user_id) \
                                  .single() \
                                  .execute()
                if wallet_response.data:
                    current_balance = wallet_response.data['balance']
                    refunded_balance = current_balance + amount_to_refund
                    supabase.table('user_wallets') \
                            .update({'balance': refunded_balance}) \
                            .eq('user_id', user_id) \
                            .execute()
                    app_logger.info(f"Refunded {amount_to_refund} to user {user_id} for failed/rejected withdrawal {transaction_id}.")
                else:
                    app_logger.warning(f"Could not find wallet for user {user_id} to refund for transaction {transaction_id}.")
            else:
                app_logger.warning(f"Could not find transaction {transaction_id} to get user_id for refund.")

        # Update the transaction status in the database
        response = supabase.table('transactions') \
                   .update(update_payload) \
                   .eq('id', transaction_id) \
                   .execute()

        if response and response.data and len(response.data) > 0:
            app_logger.info(f"Transaction {transaction_id} status updated to {new_status} by admin.")
            return jsonify({'success': True, 'message': f'Withdrawal request {transaction_id} updated to {new_status}.'}), 200
        else:
            app_logger.error(f"Failed to update transaction status for {transaction_id}. Response: {response.error if hasattr(response, 'error') and response.error else 'No data returned'}")
            return jsonify({'success': False, 'message': 'Failed to update withdrawal request status.'}), 500

    except Exception as e:
        app_logger.error(f"Error updating withdrawal status: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'An unexpected error occurred while updating status.'}), 500


# --- HELPER FUNCTION: update_withdrawal_status_internal ---
# This function is specifically designed to be called internally from other Flask routes
# to trigger the core withdrawal status update logic.
def update_withdrawal_status_internal(req):
    # Directly calls the core logic function, passing the relevant data from the "simulated request"
    return update_withdrawal_status_logic(req.json)


# --- ADMIN LOGIN & LOGOUT ROUTES ---
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session['admin_logged_in'] = True
            flash('Logged in successfully!', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Invalid credentials. Please try again.', 'danger')
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('admin_login'))


# --- ADMIN DASHBOARD ROUTES ---
@app.route('/admin/withdrawals', methods=['GET'])
@admin_required
def admin_dashboard():
    if not supabase:
        flash('Supabase client not initialized.', 'danger')
        return render_template('admin_withdrawals.html', requests=[], error='Backend setup issue.')

    try:
        # Fetch pending withdrawal requests
        # We also select 'profiles.nickname' to show user nickname
        response = supabase.table('transactions') \
            .select('*, bank_cards!inner(*), profiles!inner(nickname)') \
            .eq('type', 'withdrawal') \
            .eq('status', 'pending') \
            .order('created_at', desc=True) \
            .execute()

        if response.data:
            pending_requests = []
            for item in response.data:
                # Assuming metadata contains account details for manual transfers
                metadata = item.get('metadata', {})
                # Ensure bank_cards data is present
                bank_card_data = item.get('bank_cards')
                if bank_card_data:
                    account_holder_name = bank_card_data.get('account_holder_name', 'N/A')
                    account_number = bank_card_data.get('account_number', 'N/A')
                    ifsc_code = bank_card_data.get('ifsc_code', 'N/A')
                    bank_name = bank_card_data.get('bank_name', 'N/A')
                else:
                    # Fallback to metadata if bank_cards is not linked or missing for some reason
                    account_holder_name = metadata.get('account_holder_name', 'N/A')
                    account_number = metadata.get('account_number', 'N/A')
                    ifsc_code = metadata.get('ifsc_code', 'N/A')
                    bank_name = metadata.get('bank_name', 'N/A')

                pending_requests.append({
                    'id': item['id'],
                    'user_id': item['user_id'],
                    'nickname': item['profiles']['nickname'], # Get nickname from joined table
                    'amount': item['amount'],
                    'status': item['status'],
                    'created_at': item['created_at'],
                    'account_holder_name': account_holder_name,
                    'account_number': account_number,
                    'ifsc_code': ifsc_code,
                    'bank_name': bank_name,
                    'admin_notes': item.get('admin_notes', '') # Include existing admin notes
                })
            return render_template('admin_withdrawals.html', requests=pending_requests)
        else:
            return render_template('admin_withdrawals.html', requests=[], message='No pending withdrawal requests.')

    except Exception as e:
        app_logger.error(f"Error fetching pending withdrawals for admin dashboard: {e}", exc_info=True)
        flash(f'Error fetching withdrawals: {e}', 'danger')
        return render_template('admin_withdrawals.html', requests=[], error='Error fetching data.')

@app.route('/admin/withdrawals/process', methods=['POST'], endpoint='process_withdrawals')
@admin_required
def process_withdrawal_action():
    transaction_id = request.form.get('transaction_id')
    action = request.form.get('action') # 'complete' or 'reject'
    admin_notes = request.form.get('admin_notes_from_form', '') # Correctly get from the hidden input field

    if not all([transaction_id, action]):
        flash('Missing transaction ID or action.', 'danger')
        return redirect(url_for('admin_dashboard'))

    # Map action to status for the API endpoint
    new_status = 'completed' if action == 'complete' else 'rejected' if action == 'reject' else None

    if not new_status:
        flash('Invalid action specified.', 'danger')
        return redirect(url_for('admin_dashboard'))

    try:
        # Simulate the request object for the function
        simulated_request = type('obj', (object,), {'json': {
            'transaction_id': transaction_id,
            'status': new_status,
            'admin_notes': admin_notes
        }})()

        # Call the internal helper, which then calls the core logic
        response_tuple = update_withdrawal_status_internal(simulated_request)

        # Check the actual JSON response for success/failure (response_tuple is a (jsonify_obj, status_code) tuple)
        # Access the JSON content via .json property of the Flask Response object
        if response_tuple and isinstance(response_tuple, tuple) and response_tuple[0].json.get('success'):
            flash(response_tuple[0].json.get('message', f'Withdrawal {action}d successfully!'), 'success')
        elif response_tuple and isinstance(response_tuple, tuple):
             flash(response_tuple[0].json.get('message', f'Failed to {action} withdrawal.'), 'danger')
        else:
            flash(f'An unexpected response format was received after attempting to {action} withdrawal.', 'danger')

    except Exception as e:
        app_logger.error(f"Error processing withdrawal action: {e}", exc_info=True)
        flash(f'An unexpected error occurred: {e}', 'danger')

    return redirect(url_for('admin_dashboard'))


# --- EXISTING ROUTES (as provided in your context) ---

@app.route('/api/create-supabase-user', methods=['POST'])
def create_supabase_user():
    if not supabase_admin_auth or not supabase: # Ensure both are initialized
        app_logger.error("Supabase client not initialized in create_supabase_user.")
        return jsonify({'error': 'Backend setup issue: Supabase client not initialized'}), 500

    data = request.get_json()
    nickname = data.get('nickname')
    phone_number = data.get('phoneNumber')
    password = data.get('password')
    # New: referral_code
    referral_code_used = data.get('referral_code')
    print(f"Backend received referral_code: {referral_code_used}")
    if not all([nickname, phone_number, password]):
        return jsonify({'error': 'Nickname, phone number, and password are required.'}), 400

    referrer_id = None
    if referral_code_used:
        try:
            # Find the user who owns this referral code
            referrer_response = supabase.table('profiles') \
                                .select('id') \
                                .eq('referral_code', referral_code_used) \
                                .single() \
                                .execute()
            if referrer_response.data:
                referrer_id = referrer_response.data['id']
                app_logger.info(f"Referral code '{referral_code_used}' found, referrer ID: {referrer_id}")
            else:
                app_logger.warning(f"Invalid referral code used: {referral_code_used}")
                # Don't block registration, just don't assign referrer
        except Exception as e:
            app_logger.error(f"Error checking referral code {referral_code_used}: {e}", exc_info=True)
            # Continue without referrer if lookup fails

    try:
        user_response = supabase_admin_auth.create_user(
            {
                "phone": phone_number,
                "password": password,
                "phone_confirm": True
            }
        )

        if user_response.user is None:
            error_message_detail = user_response.dict().get('msg', 'Unknown error during user creation.')
            app_logger.error(f"Supabase create_user raw response: {user_response}")
            if 'User already exists' in error_message_detail:
                return jsonify({'error': 'This phone number is already registered. Please sign in.'}), 409
            return jsonify({'error': f'Failed to create user: {error_message_detail}'}), 500

        user_id = user_response.user.id
        app_logger.info(f"User created in auth.users: {user_id}")

        # Generate a unique referral code for the new user
        new_user_referral_code = str(uuid.uuid4()).replace('-', '')[:10].upper() # Example: 10-char UUID based
        
        profile_response = supabase.table("profiles").insert(
            {
                "id": user_id,
                "nickname": nickname,
                "phone_number": phone_number,
                "referral_code": new_user_referral_code, # Store the new user's referral code
                "referrer_id": referrer_id # Store the ID of the user who referred this new user
            }
        ).execute()

        if profile_response.data is None or len(profile_response.data) == 0:
            app_logger.error(f"Supabase profile insertion error: {profile_response.error if hasattr(profile_response, 'error') else 'No data or error'}")
            supabase_admin_auth.delete_user(user_id)
            return jsonify({'error': 'Failed to create user profile.'}), 500

        wallet_response = supabase.table("user_wallets").insert(
            {
                "user_id": user_id,
                "balance": 0.0,
                "total_income": 0.0,
                "pending_referral_bonus": 0.0, # Initialize new column
                "total_referral_earnings": 0.0 # Initialize new column
            }
        ).execute()

        if wallet_response.data is None or len(wallet_response.data) == 0:
            app_logger.error(f"Supabase wallet creation error: {wallet_response.error if hasattr(wallet_response, 'error') else 'No data or error'}")
            supabase_admin_auth.delete_user(user_id)
            # Attempt to delete profile if wallet creation fails
            try:
                supabase.table("profiles").delete().eq("id", user_id).execute()
            except Exception as delete_e:
                app_logger.error(f"Failed to clean up profile for {user_id} after wallet creation error: {delete_e}")
            return jsonify({'error': 'Failed to create user wallet.'}), 500

        # If a referrer exists, credit them with the instant bonus
        if referrer_id:
            # Increment pending_referral_bonus for the referrer
            try:
                # Use a transaction-like update for safety if Supabase supports it,
                # otherwise, fetch current and then update.
                # For simplicity, fetching current and updating.
                referrer_wallet = supabase.table('user_wallets') \
                                   .select('pending_referral_bonus') \
                                   .eq('user_id', referrer_id) \
                                   .single() \
                                   .execute()
                if referrer_wallet.data:
                    current_pending = referrer_wallet.data['pending_referral_bonus']
                    new_pending = current_pending + 10.0 # ₹10 bonus per sign-up
                    supabase.table('user_wallets') \
                            .update({'pending_referral_bonus': new_pending}) \
                            .eq('user_id', referrer_id) \
                            .execute()
                    app_logger.info(f"Credited ₹10 pending bonus to referrer {referrer_id} for new user {user_id}")
            except Exception as bonus_e:
                app_logger.error(f"Error crediting pending bonus to referrer {referrer_id}: {bonus_e}", exc_info=True)
                # This error should ideally not block new user creation, but should be logged.

        # Initialize user_quests for the new user (or just the referrer)
        # It's better to create quests for the referrer only.
        # However, if you want users to see their own quests even if not referring yet,
        # you could initialize them here too. For now, we'll assume quests are managed
        # based on referrer actions or fetched dynamically.

        app_logger.info(f"Profile and Wallet created for user: {user_id}")
        return jsonify({'message': 'Account created successfully!', 'userId': user_id, 'referralCode': new_user_referral_code}), 200

    except Exception as e:
        app_logger.error(f"An unexpected error occurred in backend: {e}", exc_info=True)
        return jsonify({'error': 'An unexpected error occurred on the server.'}), 500
    

# --- NEW: Get Invite Page Data ---
@app.route('/api/user/invite-data/<user_id>', methods=['GET'])
def get_invite_data(user_id):
    if not supabase:
        app_logger.error("Supabase client not initialized in get_invite_data.")
        return jsonify({'error': 'Backend setup issue: Supabase client not initialized'}), 500

    try:
        # 1. Fetch user's profile and wallet data
        user_data_response = supabase.table('profiles') \
                                .select('referral_code, user_wallets(pending_referral_bonus, total_referral_earnings)') \
                                .eq('id', user_id) \
                                .single() \
                                .execute()

        if not user_data_response.data:
            return jsonify({'success': False, 'message': 'User data not found.'}), 404

        profile_data = user_data_response.data
        wallet_data = profile_data.get('user_wallets')
        if not wallet_data:
            app_logger.error(f"Wallet data missing for user {user_id}")
            return jsonify({'success': False, 'message': 'User wallet data not found.'}), 500

        referral_code = profile_data.get('referral_code')
        # Ensure referral_code is generated if for some reason it's missing (shouldn't happen with current logic)
        if not referral_code:
            referral_code = str(uuid.uuid4()).replace('-', '')[:10].upper()
            supabase.table('profiles').update({'referral_code': referral_code}).eq('id', user_id).execute()


        pending_bonus = wallet_data.get('pending_referral_bonus', 0.0)
        total_referral_earnings = wallet_data.get('total_referral_earnings', 0.0)
        
        # 2. Count total direct referrals (users whose referrer_id is this user's ID)
        referred_users_response = supabase.table('profiles') \
                                  .select('id') \
                                  .eq('referrer_id', user_id) \
                                  .execute()
        
        total_referrals = len(referred_users_response.data) if referred_users_response.data else 0

        # 3. Count activated referrals (e.g., those who made a first deposit)
        # This is a critical point. You need to define "activated".
        # For this example, let's assume a referred user is "activated" if they have
        # at least one 'recharge' transaction.
        # This would typically be a more complex query, potentially involving a view.
        # For demonstration, let's count directly referred users who have ANY 'recharge' transaction.
        
        # Get IDs of users referred by current user
        direct_referral_ids = [user['id'] for user in referred_users_response.data] if referred_users_response.data else []
        
        activated_referrals_count = 0
        if direct_referral_ids:
            # Query transactions table for recharges made by direct_referral_ids
            recharge_transactions_response = supabase.table('transactions') \
                                             .select('user_id', count='exact') \
                                             .in_('user_id', direct_referral_ids) \
                                             .eq('type', 'recharge') \
                                             .eq('status', 'completed') \
                                             .execute()
            
            # Use a set to count unique user_ids from these transactions
            activated_user_ids = set()
            if recharge_transactions_response.data:
                for tx in recharge_transactions_response.data:
                    activated_user_ids.add(tx['user_id'])
            
            activated_referrals_count = len(activated_user_ids)


        # 4. Fetch/Determine Quest Bonuses status
        # First, ensure all quest types from config exist for this user in user_quests, creating if not.
        existing_quests_response = supabase.table('user_quests') \
                                   .select('quest_target, reward_amount, is_completed, is_claimed, id') \
                                   .eq('user_id', user_id) \
                                   .execute()
        existing_quests_map = {q['quest_target']: q for q in existing_quests_response.data} if existing_quests_response.data else {}

        quests_to_return = []
        for quest_config in REFERRAL_QUESTS_CONFIG:
            target = quest_config['target']
            reward = quest_config['reward']
            
            existing_quest = existing_quests_map.get(target)
            
            is_completed = False
            is_claimed = False
            quest_id = None

            if existing_quest:
                is_completed = existing_quest['is_completed']
                is_claimed = existing_quest['is_claimed']
                quest_id = existing_quest['id']
            
            # Re-evaluate completion status based on current activated_referrals_count
            if not is_completed and activated_referrals_count >= target:
                is_completed = True
                # Update in DB if status changed
                if existing_quest: # Update existing
                    supabase.table('user_quests').update({
                        'is_completed': True,
                        'completed_at': datetime.datetime.now().isoformat()
                    }).eq('id', quest_id).execute()
                else: # Insert new quest if it was just completed
                     insert_response = supabase.table('user_quests').insert({
                        'user_id': user_id,
                        'quest_target': target,
                        'reward_amount': reward,
                        'is_completed': True,
                        'is_claimed': False,
                        'completed_at': datetime.datetime.now().isoformat()
                    }).execute()
                     if insert_response.data:
                         quest_id = insert_response.data[0]['id']


            # If no existing quest, create a placeholder (or if just completed, it was inserted above)
            if not existing_quest and not is_completed: # if it wasn't completed now and didn't exist
                 insert_response = supabase.table('user_quests').insert({
                    'user_id': user_id,
                    'quest_target': target,
                    'reward_amount': reward,
                    'is_completed': False,
                    'is_claimed': False
                }).execute()
                 if insert_response.data:
                    quest_id = insert_response.data[0]['id']
                 else:
                    app_logger.error(f"Failed to initialize quest {target} for user {user_id}. Supabase error: {insert_response.error}")
                    # Continue anyway, but this quest won't be claimable until fixed in DB

            quests_to_return.append({
                'id': quest_id, # Frontend might need this for claiming
                'target': target,
                'reward': reward,
                'completed': is_completed,
                'claimed': is_claimed
            })


        return jsonify({
            'success': True,
            'referralCode': referral_code,
            'invitationLink': f"{FRONTEND_SIGNUP_BASE_URL}?ref={referral_code}",
            'totalReferrals': total_referrals,
            'currentInvites': activated_referrals_count, # This is 'currentInvites' in frontend
            'referralEarnings': total_referral_earnings,
            'pendingBonus': pending_bonus,
            'canClaimBonus': pending_bonus > 0,
            'questBonuses': quests_to_return
        }), 200

    except Exception as e:
        app_logger.error(f"Error fetching invite data for user {user_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'An unexpected error occurred while fetching invite data.'}), 500

# --- NEW: Claim Referral Bonus (₹10 per sign-up) ---
@app.route('/api/user/claim-referral-bonus', methods=['POST'])
def claim_referral_bonus():
    if not supabase:
        app_logger.error("Supabase client not initialized in claim_referral_bonus.")
        return jsonify({'success': False, 'message': 'Backend setup issue: Supabase client not initialized.'}), 500
    
    data = request.json
    user_id = data.get('userId')

    if not user_id:
        return jsonify({'success': False, 'message': 'User ID is required.'}), 400

    try:
        # Fetch current pending bonus and wallet balance
        wallet_response = supabase.table('user_wallets') \
                            .select('balance, pending_referral_bonus, total_referral_earnings') \
                            .eq('user_id', user_id) \
                            .single() \
                            .execute()
        
        if not wallet_response.data:
            return jsonify({'success': False, 'message': 'User wallet not found.'}), 404

        current_balance = wallet_response.data['balance']
        pending_bonus = wallet_response.data['pending_referral_bonus']
        total_referral_earnings = wallet_response.data['total_referral_earnings']

        if pending_bonus <= 0:
            return jsonify({'success': False, 'message': 'No pending bonus to claim.'}), 400

        # Calculate new balances
        amount_to_claim = pending_bonus
        new_balance = current_balance + amount_to_claim
        new_total_referral_earnings = total_referral_earnings + amount_to_claim

        # Update wallet: add to balance, reset pending, update total earned
        update_wallet_response = supabase.table('user_wallets').update({
            'balance': new_balance,
            'pending_referral_bonus': 0.0,
            'total_referral_earnings': new_total_referral_earnings
        }).eq('user_id', user_id).execute()

        if not update_wallet_response.data:
            app_logger.error(f"Failed to update wallet for claiming referral bonus for user {user_id}. Supabase error: {update_wallet_response.error}")
            return jsonify({'success': False, 'message': 'Failed to update wallet after claiming bonus.'}), 500

        # Record a transaction for the bonus claim
        transaction_data = {
            'user_id': user_id,
            'amount': amount_to_claim,
            'type': 'bonus_referral_signup',
            'status': 'completed',
            'description': f'Claimed ₹{amount_to_claim} referral signup bonus'
        }
        supabase.table('transactions').insert(transaction_data).execute() # Log this, but don't block response on it

        app_logger.info(f"User {user_id} claimed ₹{amount_to_claim} referral bonus.")
        return jsonify({
            'success': True,
            'message': f'₹{amount_to_claim} referral bonus claimed successfully!',
            'new_balance': new_balance,
            'new_pending_bonus': 0.0,
            'new_total_referral_earnings': new_total_referral_earnings
        }), 200

    except Exception as e:
        app_logger.error(f"Error claiming referral bonus for user {user_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'An unexpected error occurred while claiming bonus.'}), 500


# --- NEW: Claim Quest Reward ---
@app.route('/api/user/claim-quest-reward', methods=['POST'])
def claim_quest_reward():
    if not supabase:
        app_logger.error("Supabase client not initialized in claim_quest_reward.")
        return jsonify({'success': False, 'message': 'Backend setup issue: Supabase client not initialized.'}), 500
    
    data = request.json
    user_id = data.get('userId')
    quest_id = data.get('questId') # ID of the specific quest row in user_quests table

    if not all([user_id, quest_id]):
        return jsonify({'success': False, 'message': 'User ID and Quest ID are required.'}), 400

    try:
        # 1. Fetch the quest details
        quest_response = supabase.table('user_quests') \
                            .select('reward_amount, is_completed, is_claimed') \
                            .eq('id', quest_id) \
                            .eq('user_id', user_id) \
                            .single() \
                            .execute()
        
        if not quest_response.data:
            return jsonify({'success': False, 'message': 'Quest not found or does not belong to user.'}), 404
        
        quest = quest_response.data

        if not quest['is_completed']:
            return jsonify({'success': False, 'message': 'Quest not yet completed.'}), 400
        if quest['is_claimed']:
            return jsonify({'success': False, 'message': 'Quest reward already claimed.'}), 400

        reward_amount = quest['reward_amount']

        # 2. Update wallet balance
        wallet_response = supabase.table('user_wallets') \
                            .select('balance') \
                            .eq('user_id', user_id) \
                            .single() \
                            .execute()
        
        if not wallet_response.data:
            app_logger.error(f"Wallet not found for user {user_id} during quest claim for quest {quest_id}")
            return jsonify({'success': False, 'message': 'User wallet not found.'}), 404

        current_balance = wallet_response.data['balance']
        new_balance = current_balance + reward_amount

        update_wallet_response = supabase.table('user_wallets').update({
            'balance': new_balance
        }).eq('user_id', user_id).execute()

        if not update_wallet_response.data:
            app_logger.error(f"Failed to update wallet for claiming quest reward for user {user_id}. Supabase error: {update_wallet_response.error}")
            return jsonify({'success': False, 'message': 'Failed to update wallet after claiming quest reward.'}), 500
        
        # 3. Mark quest as claimed
        update_quest_response = supabase.table('user_quests').update({
            'is_claimed': True,
            'claimed_at': datetime.datetime.now().isoformat()
        }).eq('id', quest_id).execute()

        if not update_quest_response.data:
            app_logger.error(f"Failed to mark quest {quest_id} as claimed for user {user_id}. Supabase error: {update_quest_response.error}")
            # IMPORTANT: If wallet was updated but quest not marked, you have a consistency issue.
            # You might need to consider rolling back the wallet update or flagging for manual review.
            return jsonify({'success': False, 'message': 'Failed to record quest claim status.'}), 500

        # 4. Record transaction
        transaction_data = {
            'user_id': user_id,
            'amount': reward_amount,
            'type': 'bonus_quest_reward',
            'status': 'completed',
            'description': f'Claimed quest reward for {quest["quest_target"]} invites'
        }
        supabase.table('transactions').insert(transaction_data).execute()

        app_logger.info(f"User {user_id} claimed quest {quest_id} for ₹{reward_amount}.")
        return jsonify({
            'success': True,
            'message': f'Congratulations! You have claimed ₹{reward_amount} reward!',
            'new_balance': new_balance
        }), 200

    except Exception as e:
        app_logger.error(f"Error claiming quest reward for user {user_id}, quest {quest_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'An unexpected error occurred while claiming quest reward.'}), 500

# --- NEW: Create Razorpay Order Endpoint ---
# This endpoint is called by the frontend to get an order_id before opening the Razorpay popup.
@app.route('/api/recharge/create-razorpay-order', methods=['POST'])
def create_razorpay_order():
    if not razorpay_client:
        app_logger.error("Razorpay client not initialized in create_razorpay_order.")
        return jsonify({'success': False, 'message': 'Backend setup issue: Razorpay client not initialized.'}), 500

    data = request.get_json()
    amount_in_inr = data.get('amount')
    user_id = data.get('userId')

    if not all([amount_in_inr, user_id]):
        return jsonify({'success': False, 'message': 'Amount and User ID are required to create an order.'}), 400

    amount_in_paisa = int(float(amount_in_inr) * 100)

    try:
        # Generate a short, unique receipt ID using UUID
        # A UUID is 36 characters, which fits within the 40-character limit.
        # Optionally, you can prefix it with something short like 'rcpt_' if you want,
        # but the raw UUID is unique enough.
        receipt_id = str(uuid.uuid4()) # Generates a unique UUID like 'a1b2c3d4-e5f6-7890-1234-567890abcdef'
        app_logger.info(f"Generated Razorpay receipt ID: {receipt_id}")

        order_payload = {
            'amount': amount_in_paisa,
            'currency': 'INR',
            'receipt': receipt_id, # Use the generated UUID here
            'payment_capture': '1'
        }
        razorpay_order = razorpay_client.order.create(order_payload)
        app_logger.info(f"Razorpay order created: {razorpay_order['id']} for user {user_id}, amount {amount_in_inr}")
        
        # ... (rest of your create_razorpay_order logic, including pending transaction insert) ...
        pending_transaction_data = {
            'user_id': user_id,
            'type': 'recharge',
            'amount': amount_in_inr, # Store in INR
            'status': 'pending',
            'description': f'Razorpay order creation for {amount_in_inr} INR',
            'payment_gateway_id': razorpay_order['id'], # Store Razorpay order ID
            'receipt_id': receipt_id # Store the receipt ID for reference if needed
        }
        supabase.table('transactions').insert(pending_transaction_data).execute()
        app_logger.info(f"Pending transaction recorded for Razorpay order {razorpay_order['id']}")


        return jsonify({
            'success': True,
            'order_id': razorpay_order['id'],
            'amount': amount_in_inr,
            'currency': 'INR',
            'key_id': RAZORPAY_KEY_ID
        }), 200

    except Exception as e:
        app_logger.error(f"Error creating Razorpay order: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Failed to create Razorpay order: {e}'}), 500



# --- MODIFIED: Verify Razorpay Payment Endpoint ---
# This endpoint is called by the frontend AFTER the payment is made.
@app.route('/api/recharge/verify-razorpay-payment', methods=['POST'])
def verify_razorpay_payment(): # Renamed the function to avoid conflict if you had verify_razorpay_payment2
    if not razorpay_client or not supabase:
        app_logger.error("Clients not initialized in verify_razorpay_payment.")
        return jsonify({'success': False, 'message': 'Backend setup issue: Clients not initialized.'}), 500

    data = request.get_json()
    razorpay_order_id = data.get('razorpay_order_id')
    razorpay_payment_id = data.get('razorpay_payment_id')
    razorpay_signature = data.get('razorpay_signature')
    recharge_amount_inr = data.get('amount') # This amount should be in INR, not paisa, for wallet update
    user_id = data.get('userId')

    if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature, recharge_amount_inr, user_id]):
        return jsonify({'success': False, 'message': 'Missing payment details.'}), 400

    try:
        # Verify the payment signature
        # This will raise an exception if the signature is invalid
        razorpay_client.utility.verify_payment_signature({
            'razorpay_order_id': razorpay_order_id,
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_signature': razorpay_signature
        })
        app_logger.info(f"Razorpay signature verified for payment {razorpay_payment_id}")

        # --- Check if this is the user's first recharge before updating wallet ---
        is_first_recharge = False
        existing_recharges = supabase.table('transactions') \
                               .select('id') \
                               .eq('user_id', user_id) \
                               .eq('type', 'recharge') \
                               .eq('status', 'completed') \
                               .limit(1) \
                               .execute()
        
        if not existing_recharges.data or len(existing_recharges.data) == 0:
            is_first_recharge = True
            app_logger.info(f"Detected first recharge for user {user_id}.")
        # --- End of first recharge check ---

        # Fetch current wallet balance
        wallet_response = supabase.table('user_wallets').select('balance').eq('user_id', user_id).single().execute()

        if wallet_response.data is None:
            app_logger.warning(f"Wallet not found for user: {user_id}")
            return jsonify({'success': False, 'message': 'User wallet not found.'}), 404

        current_balance = wallet_response.data['balance']
        new_balance = current_balance + recharge_amount_inr # Use the INR amount directly for balance

        # Update wallet balance
        update_response = supabase.table('user_wallets').update(
            {'balance': new_balance}
        ).eq('user_id', user_id).execute()

        if update_response.data and len(update_response.data) > 0:
            app_logger.info(f"Wallet updated for {user_id}. New balance: {new_balance}")
        else:
            app_logger.error(f"Failed to update wallet for {user_id}. Supabase error: {update_response.error if hasattr(update_response, 'error') else 'Unknown'}")
            # If wallet update fails, you might want to log this as a critical error
            # and potentially refund or flag for manual review.
            raise Exception("Supabase wallet update failed after successful payment verification.")

        # Update the pending transaction status to 'completed'
        # Or create a new one if you didn't create a 'pending' one in create_razorpay_order
        update_transaction_response = supabase.table('transactions') \
            .update({'status': 'completed', 'payment_gateway_id': razorpay_payment_id}) \
            .eq('payment_gateway_id', razorpay_order_id) \
            .execute()
        
        if update_transaction_response.data and len(update_transaction_response.data) > 0:
            app_logger.info(f"Transaction status updated to completed for order {razorpay_order_id}")
        else:
            app_logger.error(f"Failed to update transaction status for order {razorpay_order_id}. Supabase error: {update_transaction_response.error if hasattr(update_transaction_response, 'error') else 'Unknown'}")
            # This is also a critical consistency issue.

        # --- NEW: Logic for 'activated' referral and quest check ---
        if is_first_recharge:
            # Get the referrer's ID for this user
            referred_user_profile_response = supabase.table('profiles') \
                                             .select('referrer_id') \
                                             .eq('id', user_id) \
                                             .single() \
                                             .execute()
            
            referrer_id = None
            if referred_user_profile_response.data and referred_user_profile_response.data['referrer_id']:
                referrer_id = referred_user_profile_response.data['referrer_id']
                app_logger.info(f"User {user_id} made first recharge. Referrer is {referrer_id}.")

                # Trigger a refresh of the referrer's invite data to update quest progress
                # This could be more sophisticated (e.g., a Supabase Function trigger)
                # For now, we'll just log that an activation occurred.
                # The '/api/user/invite-data' endpoint will recalculate quest completion.
                app_logger.info(f"First recharge by referred user {user_id}. Referrer {referrer_id}'s quest progress may have updated.")

        # --- End of NEW logic ---

        return jsonify({'success': True, 'message': 'Recharge successful and wallet updated!', 'new_balance': new_balance})

    except razorpay.errors.SignatureVerificationError as e:
        app_logger.error(f"Razorpay Signature Verification Failed: {e}", exc_info=True)
        # If verification fails, update the transaction status to 'failed'
        supabase.table('transactions') \
            .update({'status': 'failed', 'description': f'Payment verification failed: {e}'}) \
            .eq('payment_gateway_id', razorpay_order_id) \
            .execute()
        return jsonify({'success': False, 'message': 'Payment verification failed: Invalid signature.'}), 400
    except Exception as e:
        app_logger.error(f"Internal Server Error during payment verification: {e}", exc_info=True)
        # If any other error occurs, update the transaction status to 'failed'
        supabase.table('transactions') \
            .update({'status': 'failed', 'description': f'Internal server error during verification: {e}'}) \
            .eq('payment_gateway_id', razorpay_order_id) \
            .execute()
        return jsonify({'success': False, 'message': f'Payment verification failed due to an unexpected error.'}), 500


@app.route('/api/user/add-bank-card', methods=['POST'])
def add_bank_card():
    try:
        data = request.json
        user_id = data.get('user_id')
        account_number = data.get('account_number')
        bank_name = data.get('bank_name')
        ifsc_code = data.get('ifsc_code')
        account_holder_name = data.get('account_holder_name')

        if not all([user_id, account_number, bank_name, ifsc_code, account_holder_name]):
            return jsonify({'success': False, 'message': 'Missing required bank card details.'}), 400

        # Basic IFSC validation
        if not (isinstance(ifsc_code, str) and len(ifsc_code) == 11 and ifsc_code.isalnum() and ifsc_code[0:4].isalpha() and ifsc_code[4] == '0' and ifsc_code[5:].isalnum()):
            return jsonify({'success': False, 'message': 'Invalid IFSC Code format.'}), 400

        response = supabase.table('bank_cards').insert({
            'user_id': user_id,
            'account_number': account_number,
            'bank_name': bank_name,
            'ifsc_code': ifsc_code,
            'account_holder_name': account_holder_name,
            'is_verified': False,
            'razorpay_fund_account_id': None # Keep this column, it's harmless and can be used later if you get RazorpayX
        }).execute()

        if response and response.data and len(response.data) > 0:
            return jsonify({
                'success': True,
                'message': 'Bank card added successfully!',
                'bank_card_id': response.data[0]['id']
            }), 201
        else:
            app_logger.error(f"Supabase insert returned no data unexpectedly for bank card. Response: {response.error if hasattr(response, 'error') else 'No data or error'}")
            return jsonify({'success': False, 'message': 'Failed to save bank card. No data returned from Supabase.'}), 500

    except Exception as e:
        app_logger.error(f"Error adding bank card: {e}", exc_info=True)
        error_message = 'An unexpected error occurred.'
        if hasattr(e, 'message'):
            error_message = e.message
        elif hasattr(e, 'response') and hasattr(e.response, 'text'):
            try:
                error_json = e.response.json()
                error_message = error_json.get('message', error_message)
            except:
                error_message = e.response.text
        elif str(e):
            error_message = str(e)

        return jsonify({'success': False, 'message': f'Failed to add bank card: {error_message}'}), 500

@app.route('/api/user/bank-cards/<user_id>', methods=['GET'])
def get_user_bank_cards(user_id):
    try:
        response = supabase.table('bank_cards').select('*').eq('user_id', user_id).execute()

        if response.data is None:
            return jsonify({'success': True, 'bank_cards': []}), 200

        # Note: response.count is typically for queries with .limit() or .range() and .count() methods.
        # For a simple select without count, checking response.data is usually sufficient.
        if response.error: # Check for a direct error from Supabase
            app_logger.error(f"Supabase error fetching bank cards: {response.error.message}")
            return jsonify({'success': False, 'message': 'Database error fetching bank cards.'}), 500

        return jsonify({'success': True, 'bank_cards': response.data}), 200

    except Exception as e:
        app_logger.error(f"Error fetching bank cards for user {user_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'An unexpected error occurred.'}), 500

@app.route('/api/user/set-trade-password', methods=['POST'])
def set_trade_password():
    try:
        data = request.json
        user_id = data.get('user_id')
        new_trade_password = data.get('new_trade_password')

        if not all([user_id, new_trade_password]):
            return jsonify({'success': False, 'message': 'User ID and new trade password are required.'}), 400

        if len(new_trade_password) < 6:
            return jsonify({'success': False, 'message': 'Trade password must be at least 6 characters long.'}), 400

        hashed_trade_password = generate_password_hash(new_trade_password)

        response = supabase.table('profiles').update({
            'trade_password_hash': hashed_trade_password
        }).eq('id', user_id).execute()

        if response and response.data and len(response.data) > 0:
            return jsonify({'success': True, 'message': 'Trade password set successfully!'}), 200
        else:
            app_logger.error(f"Supabase update returned no data unexpectedly for trade password. Response: {response.error if hasattr(response, 'error') else 'No data or error'}")
            return jsonify({'success': False, 'message': 'Failed to set trade password. No data returned from Supabase.'}), 500

    except Exception as e:
        app_logger.error(f"Error setting trade password: {e}", exc_info=True)
        error_message = 'An unexpected error occurred.'
        if hasattr(e, 'message'):
            error_message = e.message
        elif hasattr(e, 'response') and hasattr(e.response, 'text'):
            try:
                error_json = e.response.json()
                error_message = error_json.get('message', error_message)
            except:
                error_message = e.response.text
        elif str(e):
            error_message = str(e)

        return jsonify({'success': False, 'message': f'Failed to set trade password: {error_message}'}), 500

@app.route('/api/user/verify-password', methods=['POST'])
def verify_user_password():
    try:
        data = request.json
        user_id = data.get('userId')
        submitted_trade_password = data.get('password')

        if not all([user_id, submitted_trade_password]):
            return jsonify({'success': False, 'message': 'User ID and trade password are required.'}), 400

        try:
            profile_response = supabase.table('profiles').select('trade_password_hash').eq('id', user_id).single().execute()
        except APIError as e:
            if e.code == 'PGRST116': # Not Found error code for PostgREST
                app_logger.warning(f"Trade password not set for user {user_id}. No profile found or no hash.")
                return jsonify({'success': False, 'message': 'Trade password not set for user.'}), 404
            else:
                app_logger.error(f"Supabase API error fetching profile for trade password verification: {e.message}", exc_info=True)
                return jsonify({'success': False, 'message': f'Database error during trade password verification: {e.message}'}), 500
        except Exception as e:
            app_logger.error(f"Unexpected error during Supabase call for trade password verification: {e}", exc_info=True)
            return jsonify({'success': False, 'message': 'An unexpected database error occurred.'}), 500

        if profile_response.data and profile_response.data['trade_password_hash']:
            stored_trade_password_hash = profile_response.data['trade_password_hash']
            if check_password_hash(stored_trade_password_hash, submitted_trade_password):
                return jsonify({'success': True, 'message': 'Trade password verified.'}), 200
            else:
                return jsonify({'success': False, 'message': 'Invalid trade password.'}), 401
        else:
            app_logger.warning(f"Trade password hash is NULL for user {user_id}.")
            return jsonify({'success': False, 'message': 'Trade password not set for user.'}), 404

    except Exception as e:
        app_logger.error(f"Error verifying trade password (general exception): {e}", exc_info=True)
        error_message = 'An unexpected error occurred during trade password verification.'
        if hasattr(e, 'message'):
            error_message = e.message
        elif hasattr(e, 'response') and hasattr(e.response, 'text'):
            try:
                error_json = e.response.json()
                error_message = error_json.get('message', error_message)
            except:
                error_message = e.response.text
        elif str(e):
            error_message = str(e)

        return jsonify({'success': False, 'message': f'Trade password verification failed: {error_message}'}), 500


@app.route('/api/withdrawal/request', methods=['POST'])
def handle_withdrawal_request():
    try:
        data = request.json
        user_id = data.get('userId')
        amount = data.get('amount') # Amount in INR
        bank_card_id = data.get('bankCardId')
        bank_details = data.get('bankDetails') # Contains account_number, ifsc_code, bank_name, account_holder_name

        if not all([user_id, amount, bank_card_id, bank_details]):
            return jsonify({'success': False, 'message': 'Missing withdrawal details.'}), 400

        # Amount validation
        if not isinstance(amount, (int, float)) or amount <= 0:
            return jsonify({'success': False, 'message': 'Invalid withdrawal amount.'}), 400

        # 1. Fetch current balance and check
        wallet_response = supabase.table('user_wallets').select('balance').eq('user_id', user_id).single().execute()
        if not wallet_response.data:
            app_logger.warning(f"User wallet not found for withdrawal for user: {user_id}")
            return jsonify({'success': False, 'message': 'User wallet not found.'}), 404

        current_balance = wallet_response.data['balance']
        if current_balance < amount:
            return jsonify({'success': False, 'message': 'Insufficient balance for withdrawal.'}), 400

        # 2. Deduct amount from wallet immediately
        new_balance = current_balance - amount
        wallet_update_response = supabase.table('user_wallets').update({'balance': new_balance}).eq('user_id', user_id).execute()

        if not wallet_update_response.data or len(wallet_update_response.data) == 0:
            app_logger.error(f"Failed to update wallet balance for withdrawal for user {user_id}. Supabase response: {wallet_update_response.error if hasattr(wallet_update_response, 'error') else 'No data or error'}")
            return jsonify({'success': False, 'message': 'Failed to update wallet balance for withdrawal.'}), 500

        app_logger.info(f"Wallet updated for {user_id}. New balance: {new_balance}. Recording withdrawal request.")

        # 3. Record Transaction with 'pending' status
        transaction_data = {
            'user_id': user_id,
            'amount': amount, # Store in INR for your records
            'type': 'withdrawal',
            'status': 'pending', # Set to pending for manual processing
            'description': f"Withdrawal request for {amount} INR (manual processing)",
            'bank_card_id': bank_card_id, # <--- ENSURE THIS IS POPULATED HERE AND LINKED IN DB
            'metadata': { # Still useful for redundant storage or extra info not covered by direct columns
                'account_holder_name': bank_details['account_holder_name'],
                'bank_name': bank_details['bank_name'],
                'account_number': bank_details['account_number'], # Store full account number for manual transfer
                'ifsc_code': bank_details['ifsc_code']
            }
        }
        transaction_response = supabase.table('transactions').insert(transaction_data).execute()

        if not transaction_response.data or len(transaction_response.data) == 0:
            app_logger.error(f"Failed to record pending withdrawal transaction for user {user_id}. Supabase response: {transaction_response.error if hasattr(transaction_response, 'error') else 'No data or error'}")
            # CRITICAL: If this happens, wallet was deducted but no transaction. Refund the wallet.
            app_logger.error(f"Attempting to refund {amount} to user {user_id} due to transaction record failure.")
            supabase.table('user_wallets').update({'balance': current_balance}).eq('user_id', user_id).execute() # Simplified refund
            return jsonify({'success': False, 'message': 'Failed to record withdrawal request after wallet deduction. Amount refunded to wallet. Please try again or contact support.'}), 500

        app_logger.info(f"Withdrawal request recorded as pending for user {user_id}. Transaction ID: {transaction_response.data[0]['id']}")

        return jsonify({
            'success': True,
            'message': f'Withdrawal request for ₹{amount} submitted. It is now pending manual processing.',
            'new_balance': new_balance,
            'transaction_id': transaction_response.data[0]['id']
        }), 200

    except Exception as e:
        app_logger.error(f"Unhandled error in withdrawal request for user {user_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'An unexpected error occurred during withdrawal request submission.'}), 500


# --- Main execution block ---
if __name__ == '__main__':
    PORT = int(os.environ.get("PORT", 5000))
    app_logger.info(f"Flask app starting on port {PORT}")
    app.run(host='0.0.0.0', debug=True, port=PORT)