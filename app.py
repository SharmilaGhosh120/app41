# -*- coding: utf-8 -*-
import os
import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, date
import requests
import logging
import uuid
import re
import csv
import io
from passlib.hash import pbkdf2_sha256
from dotenv import load_dotenv
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import Table, TableStyle, SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
from ratelimit import limits, sleep_and_retry

# Load environment variables
load_dotenv()
DB_PATH = os.getenv('DB_PATH', 'kyra.db')
API_URL = "http://kyra.kyras.in:8000/student_query"

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Rate Limiting Configuration
CALLS = 10
PERIOD = 60

# Helper function to format timestamps
def format_timestamp(timestamp):
    try:
        return datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").strftime("%b %d, %Y %H:%M")
    except ValueError:
        return timestamp

# Authentication Functions
def hash_password(password):
    return pbkdf2_sha256.hash(password)

def verify_user(email, password):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT password_hash, name, role FROM users WHERE email = ?', (email,))
        result = c.fetchone()
        conn.close()
        if not result:
            return None, "Email not found."
        stored_hash = result[0]
        if pbkdf2_sha256.verify(password, stored_hash):
            return {"name": result[1], "role": result[2]}, None
        return None, "Incorrect password."
    except sqlite3.Error as e:
        logger.error(f"Database error in verify_user: {str(e)}")
        return None, "Database error. Please try again."

def reset_user_password(email, new_password):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        password_hash = hash_password(new_password)
        c.execute('UPDATE users SET password_hash = ? WHERE email = ?', (password_hash, email))
        if c.rowcount == 0:
            conn.close()
            return False, "User not found."
        conn.commit()
        conn.close()
        return True, "Password reset successfully."
    except sqlite3.Error as e:
        logger.error(f"Database error in reset_user_password: {str(e)}")
        return False, "Database error. Please try again."

def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email) is not None

# Database Utility Functions
def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # Users table
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                name TEXT,
                role TEXT,
                password_hash TEXT
            )
        ''')

        c.execute("PRAGMA table_info(users)")
        columns = [info[1] for info in c.fetchall()]
        if 'password_hash' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN password_hash TEXT')

        # Enhanced projects table
        c.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                email TEXT,
                project_title TEXT,
                timestamp TEXT,
                FOREIGN KEY (email) REFERENCES users(email)
            )
        ''')

        # Enhanced queries table with project mapping
        c.execute('''
            CREATE TABLE IF NOT EXISTS queries (
                query_id TEXT PRIMARY KEY,
                email TEXT,
                name TEXT,
                project_title TEXT,
                question TEXT,
                response TEXT,
                timestamp TEXT,
                feedback_rating INTEGER,
                FOREIGN KEY (email) REFERENCES users(email)
            )
        ''')

        # Student project mapping table
        c.execute('''
            CREATE TABLE IF NOT EXISTS student_project_mapping (
                student_email TEXT PRIMARY KEY,
                project_title TEXT,
                mapped_timestamp TEXT
            )
        ''')

        # Enhanced student_project_map table
        c.execute('''
            CREATE TABLE IF NOT EXISTS student_project_map (
                student_id TEXT,
                project_id TEXT,
                timestamp TEXT,
                PRIMARY KEY (student_id, project_id),
                FOREIGN KEY (student_id) REFERENCES users(email),
                FOREIGN KEY (project_id) REFERENCES projects(project_id)
            )
        ''')

        # Insert default admin
        default_admin = ('admin@college.edu', 'Jane Admin', 'admin', hash_password('default123'))
        c.execute('INSERT OR IGNORE INTO users (email, name, role, password_hash) VALUES (?, ?, ?, ?)', default_admin)
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
    except sqlite3.Error as e:
        logger.error(f"Database initialization error: {str(e)}")
        st.error("Failed to initialize database. Please try again.")

def save_user(email, name, password=None, role="student"):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        password_hash = hash_password(password) if password else None
        if password:
            c.execute('INSERT OR REPLACE INTO users (email, name, role, password_hash) VALUES (?, ?, ?, ?)',
                     (email, name, role, password_hash))
        else:
            c.execute('UPDATE users SET name = ?, role = ? WHERE email = ?',
                     (name, role, email))
        conn.commit()
        conn.close()
        return True, "User saved successfully."
    except sqlite3.Error as e:
        logger.error(f"Database error in save_user: {str(e)}")
        return False, "Database error. Please try again."

def get_project_title_for_student(email):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT project_title FROM student_project_mapping WHERE student_email = ?', (email,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else None
    except sqlite3.Error as e:
        logger.error(f"Database error in get_project_title_for_student: {str(e)}")
        return None

def save_query(email, name, project_title, question, response, timestamp, feedback_rating=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        query_id = str(uuid.uuid4())

        # If project_title is None, try to get from mapping
        if not project_title:
            project_title = get_project_title_for_student(email) or "No Project Assigned"

        c.execute('SELECT * FROM queries WHERE email = ? AND question = ? AND timestamp = ?', (email, question, timestamp))
        if not c.fetchone():
            c.execute('INSERT INTO queries (query_id, email, name, project_title, question, response, timestamp, feedback_rating) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                     (query_id, email, name, project_title, question, response, timestamp, feedback_rating))
            conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Database error in save_query: {str(e)}")
        st.error("Failed to save query. Please try again.")

def save_project(email, project_title):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        project_id = str(uuid.uuid4())
        c.execute('INSERT INTO projects (project_id, email, project_title, timestamp) VALUES (?, ?, ?, ?)',
                 (project_id, email, project_title, timestamp))
        c.execute('INSERT INTO student_project_map (student_id, project_id, timestamp) VALUES (?, ?, ?)',
                 (email, project_id, timestamp))

        # Also update the mapping table
        c.execute('INSERT OR REPLACE INTO student_project_mapping (student_email, project_title, mapped_timestamp) VALUES (?, ?, ?)',
                 (email, project_title, timestamp))

        conn.commit()
        conn.close()
        return True, "Project saved successfully."
    except sqlite3.Error as e:
        logger.error(f"Database error in save_project: {str(e)}")
        return False, "Database error. Please try again."

def get_user_projects(email):
    try:
        conn = sqlite3.connect(DB_PATH)
        user_projects = pd.read_sql_query("SELECT project_title, timestamp FROM projects WHERE email = ? ORDER BY timestamp DESC",
                                        conn, params=(email,))
        conn.close()
        return user_projects
    except sqlite3.Error as e:
        logger.error(f"Database error in get_user_projects: {str(e)}")
        return pd.DataFrame()

def delete_user(email):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM users WHERE email = ?', (email,))
        if c.rowcount == 0:
            conn.close()
            return False, "User not found."
        # Also clean up related data
        c.execute('DELETE FROM student_project_mapping WHERE student_email = ?', (email,))
        c.execute('DELETE FROM queries WHERE email = ?', (email,))
        c.execute('DELETE FROM projects WHERE email = ?', (email,))
        conn.commit()
        conn.close()
        return True, "User deleted successfully."
    except sqlite3.Error as e:
        logger.error(f"Database error in delete_user: {str(e)}")
        return False, "Database error. Please try again."

def get_dashboard_stats():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # Total students
        c.execute("SELECT COUNT(DISTINCT email) FROM users WHERE role = 'student'")
        total_students = c.fetchone()[0]

        # Total projects from mapping
        c.execute("SELECT COUNT(DISTINCT project_title) FROM student_project_mapping")
        total_projects = c.fetchone()[0]

        # Total queries
        c.execute("SELECT COUNT(*) FROM queries")
        total_queries = c.fetchone()[0]

        # Average rating
        c.execute("SELECT AVG(feedback_rating) FROM queries WHERE feedback_rating IS NOT NULL")
        avg_rating = c.fetchone()[0]

        conn.close()
        return {
            'total_students': total_students,
            'total_projects': total_projects,
            'total_queries': total_queries,
            'avg_ratings': round(avg_rating, 2) if avg_rating else None
        }
    except sqlite3.Error as e:
        logger.error(f"Database error in get_dashboard_stats: {str(e)}")
        return {'total_students': 0, 'total_projects': 0, 'total_queries': 0, 'avg_rating': None}

def export_query_logs_to_csv(student_email=None, project_filter=None, date_from=None, date_to=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        query = "SELECT email, name, project_title, question, response, timestamp, feedback_rating FROM queries WHERE 1=1"
        params = []

        if student_email:
            query += " AND email = ?"
            params.append(student_email)

        if project_filter and project_filter != "All Projects":
            query += " AND project_title = ?"
            params.append(project_filter)

        if date_from:
            query += " AND DATE(timestamp) >= ?"
            params.append(date_from.strftime("%Y-%m-%d"))

        if date_to:
            query += " AND DATE(timestamp) <= ?"
            params.append(date_to.strftime("%Y-%m-%d"))

        query += " ORDER BY timestamp DESC"

        if params:
            query_logs = pd.read_sql_query(query, conn, params=params)
        else:
            query_logs = pd.read_sql_query(query, conn)

        conn.close()
        if not query_logs.empty:
            return query_logs.to_csv(index=False).encode('utf-8')
        return None
    except sqlite3.Error as e:
        logger.error(f"Database error in export_query_logs_to_csv: {str(e)}")
        return None

def export_query_logs_to_pdf(student_email=None, project_filter=None, date_from=None, date_to=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        query = "SELECT email, name, project_title, question, response, timestamp, feedback_rating FROM queries WHERE 1=1"
        params = []

        if student_email:
            query += " AND email = ?"
            params.append(student_email)

        if project_filter and project_filter != "All Projects":
            query += " AND project_title = ?"
            params.append(project_filter)

        if date_from:
            query += " AND DATE(timestamp) >= ?"
            params.append(date_from.strftime("%Y-%m-%d"))

        if date_to:
            query += " AND DATE(timestamp) <= ?"
            params.append(date_to.strftime("%Y-%m-%d"))

        query += " ORDER BY timestamp DESC"

        if params:
            query_logs = pd.read_sql_query(query, conn, params=params)
        else:
            query_logs = pd.read_sql_query(query, conn)

        conn.close()

        if query_logs.empty:
            logger.warning("No query logs found for PDF export.")
            return None

        # Prepare data for the table
        data = [["Email", "Name", "Project", "Question", "Response", "Timestamp", "Rating"]]
        for _, row in query_logs.iterrows():
            rating = str(row['feedback_rating']) if pd.notna(row['feedback_rating']) else "Not rated"
            data.append([
                row['email'],
                row['name'],
                row['project_title'],
                row['question'][:50] + "..." if len(str(row['question'])) > 50 else str(row['question']),
                row['response'][:100] + "..." if len(str(row['response'])) > 100 else str(row['response']),
                row['timestamp'],
                rating
            ])

        # Create PDF in memory
        buffer = io.BytesIO()
        try:
            doc = SimpleDocTemplate(buffer, pagesize=letter)
            style = getSampleStyleSheet()
            elements = []
            elements.append(Paragraph("Query Logs Report", style['Title']))

            # Adjust column widths
            col_widths = [80, 80, 80, 100, 120, 80, 60]
            table = Table(data, colWidths=col_widths, repeatRows=1)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.whitesmoke),
                ('GRID', (0, 0), (-1, -1), 1, colors.grey),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
            ]))
            elements.append(table)
            doc.build(elements)
            pdf_data = buffer.getvalue()
            buffer.close()
            return pdf_data
        except Exception as e:
            logger.error(f"Error generating PDF: {str(e)}")
            buffer.close()
            return None
    except sqlite3.Error as e:
        logger.error(f"Database error in export_query_logs_to_pdf: {str(e)}")
        return None

def bulk_register_users(csv_file):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        reader = csv.DictReader(io.StringIO(csv_file.read().decode('utf-8')))
        required_columns = ['email', 'name', 'role']
        if not all(col in reader.fieldnames for col in required_columns):
            return False, "CSV must contain 'email', 'name', and 'role' columns."

        users_added = 0
        for row in reader:
            email = row['email'].strip()
            name = row['name'].strip()
            role = row['role'].strip()
            if not email or not name or not role:
                return False, f"Missing data in row: {email}"
            if not is_valid_email(email):
                return False, f"Invalid email format: {email}"
            if role not in ['student', 'admin']:
                return False, f"Invalid role for {email}: {role}"
            password = 'default123'
            password_hash = hash_password(password)
            c.execute('INSERT OR IGNORE INTO users (email, name, role, password_hash) VALUES (?, ?, ?, ?)',
                     (email, name, role, password_hash))
            if c.rowcount > 0:
                users_added += 1

        conn.commit()
        conn.close()
        return True, f"{users_added} users registered successfully!"
    except Exception as e:
        logger.error(f"Error processing CSV: {str(e)}")
        return False, f"Error processing CSV: {str(e)}"

def save_student_project_mapping(csv_file):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # Read CSV
        df = pd.read_csv(csv_file)
        required_columns = ['student_id', 'project_title']

        if not all(col in df.columns for col in required_columns):
            return False, "CSV must contain 'student_id' and 'project_title' columns."

        if df.empty:
            return False, "CSV file is empty."

        # Check for missing values
        if df['student_id'].isnull().any() or df['project_title'].isnull().any():
            return False, "CSV contains missing values in required columns."

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mappings_added = 0

        for _, row in df.iterrows():
            student_email = str(row['student_id']).strip()
            project_title = str(row['project_title']).strip()

            if not is_valid_email(student_email):
                return False, f"Invalid email format: {student_email}"

            # Insert or update mapping
            c.execute('''INSERT OR REPLACE INTO student_project_mapping
                        (student_email, project_title, mapped_timestamp) VALUES (?, ?, ?)''',
                     (student_email, project_title, timestamp))

            # Also create project entry
            project_id = str(uuid.uuid4())
            c.execute('''INSERT OR IGNORE INTO projects
                        (project_id, email, project_title, timestamp) VALUES (?, ?, ?, ?)''',
                     (project_id, student_email, project_title, timestamp))

            mappings_added += 1

        conn.commit()
        conn.close()
        return True, f"{mappings_added} student-project mappings saved successfully!"
    except Exception as e:
        logger.error(f"Error saving student project mapping: {str(e)}")
        return False, f"Error processing mapping file: {str(e)}"

def get_available_projects():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT DISTINCT project_title FROM student_project_mapping ORDER BY project_title")
        projects = [row[0] for row in c.fetchall()]
        conn.close()
        return projects
    except sqlite3.Error as e:
        logger.error(f"Database error in get_available_projects: {str(e)}")
        return []

@sleep_and_retry
@limits(calls=CALLS, period=PERIOD)
def kyra_response(email, query, project_title=None):
    try:
        headers = {"Content-Type": "application/json"}
        payload = {
            "question": query.strip(),
            "email": email.strip(),
            "project_title": project_title or "No Project Assigned"
        }
        logger.debug(f"Sending API request to {API_URL} with payload: {payload}")
        response = requests.post(API_URL, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"API response: {data}")
        # Validate response structure
        if not isinstance(data, dict) or "response" not in data:
            logger.error("Invalid API response structure")
            return "Error: Invalid response structure from API."
        return data["response"] or "No response received from API."
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error in API request: {str(e)}")
        return f"Error: API request failed with status {e.response.status_code}. Please try again later."
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error in API request: {str(e)}")
        return "Error: Unable to connect to the API. Please check your network connection."
    except requests.exceptions.Timeout as e:
        logger.error(f"API request timed out: {str(e)}")
        return "Error: API request timed out. Please try again later."
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {str(e)}")
        return f"Error: Unable to get response from API. Details: {str(e)}"
    except ValueError as e:
        logger.error(f"JSON decode error in API response: {str(e)}")
        return "Error: Invalid response format from API."

# Streamlit page configuration
st.set_page_config(
    page_title="Ask Ky'ra",
    page_icon="https://raw.githubusercontent.com/SharmilaGhosh120/app16/main/WhatsApp%20Image%202025-05-20%20at%2015.17.59.jpeg",
    layout="centered"
)

# Custom CSS for styling
st.markdown(
    """
    <style>
    .main {
        background-color: #ffffff;
        padding: 20px;
        border-radius: 10px;
        font-family: 'Roboto', sans-serif;
    }
    .stTextInput > div > input {
        border: 1px solid #ccc;
        border-radius: 5px;
        font-family: 'Roboto', sans-serif;
    }
    .stTextArea > div > textarea {
        border: 1px solid #ccc;
        border-radius: 5px;
        font-family: 'Roboto', sans-serif;
    }
    .submit-button {
        display: flex;
        justify-content: center;
    }
    .submit-button .stButton > button {
        background-color: #4fb8ac;
        color: white;
        font-size: 18px;
        padding: 10px 20px;
        border-radius: 8px;
        width: 200px;
        font-family: 'Roboto', sans-serif;
    }
    .history-entry {
        padding: 15px;
        border: 1px solid #e0e0e0;
        border-radius: 12px;
        background-color: white;
        margin-bottom: 10px;
        box-shadow: 1px 1px 3px #ccc;
        font-family: 'Roboto', sans-serif;
        word-wrap: break-word;
        overflow-wrap: break-word;
        max-width: 100%;
    }
    .chat-container {
        max-height: 400px;
        overflow-y: auto;
        padding: 10px;
        border: 1px solid #e0e0e0;
        border-radius: 12px;
        background-color: #f9f9f9;
    }
    .chat-footer {
        text-align: center;
        font-family: 'Roboto', sans-serif;
        color: #4fb8ac;
        margin-top: 20px;
    }
    .avatar {
        display: inline-block;
        width: 30px;
        height: 30px;
        border-radius: 50%;
        background-color: #4fb8ac;
        color: white;
        text-align: center;
        line-height: 30px;
        font-family: 'Roboto', sans-serif;
        margin-right: 10px;
    }
    .metric-card {
        background-color: #f8f9fa;
        padding: 15px;
        border-radius: 10px;
        border-left: 4px solid #4fb8ac;
        margin-bottom: 10px;
    }
    .refresh-indicator {
        background-color: #d4edda;
        color: #155724;
        padding: 10px;
        border-radius: 5px;
        margin: 10px 0;
        border: 1px solid #c3e6cb;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# Initialize database
init_db()

# Display logo
with st.container():
    logo_url = "https://raw.githubusercontent.com/SharmilaGhosh120/app16/main/WhatsApp%20Image%202025-05-20%20at%2015.17.59.jpeg"
    try:
        response = requests.head(logo_url, timeout=5)
        if response.status_code == 200:
            st.image(logo_url, width=100, use_container_width=True, caption="Ky'ra Logo", output_format="JPEG")
        else:
            logger.error("Unable to load Ky'ra logo. URL inaccessible.")
            st.warning("Unable to load Ky'ra logo. The image URL is inaccessible.")
    except Exception as e:
        logger.error(f"Failed to load logo: {str(e)}")
        st.error("Unable to load Ky'ra logo. Check your connection or try again later.")

# Initialize session state
if "email" not in st.session_state:
    st.session_state.email = ""
if "name" not in st.session_state:
    st.session_state.name = ""
if "role" not in st.session_state:
    st.session_state.role = ""
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "page" not in st.session_state:
    st.session_state.page = 1
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "refresh_trigger" not in st.session_state:
    st.session_state.refresh_trigger = 0

# Logout Functionality
if st.session_state.authenticated:
    if st.button("Logout"):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.success("You have been logged out successfully!")
        st.rerun()

# Login interface
if not st.session_state.authenticated:
    st.subheader("Login to Ask Ky'ra")
    email_input = st.text_input("Email", placeholder="admin@college.edu")
    password_input = st.text_input("Password", type="password", placeholder="Enter your password")
    if st.button("Login"):
        if email_input and password_input:
            user_info, error = verify_user(email_input, password_input)
            if user_info:
                st.session_state.authenticated = True
                st.session_state.email = email_input
                st.session_state.name = user_info["name"]
                st.session_state.role = user_info["role"]
                st.success(f"Login successful! Welcome, {user_info['name']}!")
                st.rerun()
            else:
                st.error(error)
        else:
            st.error("Please enter both email and password.")
else:
    # User Details Section
    st.subheader("Your Details")
    email_input = st.text_input("Email", value=st.session_state.email, placeholder="student123@college.edu", disabled=True)
    name_input = st.text_input("Your Name", value=st.session_state.name, placeholder="Enter your name")
    password_input = st.text_input("New Password (optional)", type="password", placeholder="Set or update password")

    if st.button("Update Details"):
        if name_input:
            success, message = save_user(st.session_state.email, name_input, password_input, st.session_state.role)
            if success:
                st.session_state.name = name_input
                st.success(message)
            else:
                st.error(message)
        else:
            st.error("Please enter your name.")

    # Admin-specific features
    if st.session_state.role == "admin":
        st.subheader("Manage Users")
        new_email = st.text_input("New User Email", placeholder="newstudent@college.edu")
        new_name = st.text_input("New User Name", placeholder="Enter new user name")
        new_role = st.selectbox("Role", ["student", "admin"])
        new_password = st.text_input("New User Password", type="password", placeholder="Set password")
        if st.button("Register User"):
            if not new_email or not new_name or not new_password:
                st.error("Please fill in all fields for the new user.")
            elif not is_valid_email(new_email):
                st.error("Please enter a valid email address.")
            else:
                success, message = save_user(new_email, new_name, new_password, new_role)
                if success:
                    st.success(f"{message} User: {new_name} ({new_email})")
                    st.session_state.refresh_trigger += 1
                else:
                    st.error(message)

        st.subheader("Reset User Password")
        reset_email = st.text_input("User Email to Reset", placeholder="Enter user email")
        reset_password = st.text_input("New Password", type="password", placeholder="Enter new password")
        if st.button("Reset Password"):
            if not reset_email or not reset_password:
                st.error("Please provide both email and new password.")
            elif not is_valid_email(reset_email):
                st.error("Please enter a valid email address.")
            else:
                if st.checkbox("Confirm password reset"):
                    success, message = reset_user_password(reset_email, reset_password)
                    if success:
                        st.success(f"{message} for {reset_email}")
                    else:
                        st.error(message)
                else:
                    st.warning("Please confirm the password reset.")

        st.subheader("Delete User")
        conn = sqlite3.connect(DB_PATH)
        users = pd.read_sql_query("SELECT email, name FROM users WHERE role = 'student'", conn)
        conn.close()
        if not users.empty:
            delete_email = st.selectbox("Select User to Delete", users['email'].tolist(),
                                       format_func=lambda x: f"{x} ({users[users['email'] == x]['name'].iloc[0]})")
            if st.button("Delete User"):
                if st.checkbox("Confirm deletion"):
                    success, message = delete_user(delete_email)
                    if success:
                        st.success(f"{message} User: {delete_email}")
                        st.session_state.refresh_trigger += 1
                    else:
                        st.error(message)
                else:
                    st.warning("Please confirm the deletion.")
        else:
            st.info("No student users found.")

        st.subheader("Bulk Register Users")
        uploaded_file = st.file_uploader("Upload CSV with user data (email, name, role)", type=["csv"])
        if uploaded_file and st.button("Process Bulk Registration"):
            success, message = bulk_register_users(uploaded_file)
            if success:
                st.success(message)
                st.session_state.refresh_trigger += 1
            else:
                st.error(message)

        st.subheader("Map Students to Projects")
        mapping_file = st.file_uploader("Upload CSV with mapping data (student_id, project_title)", type=["csv"])
        if mapping_file and st.button("Process Mapping"):
            success, message = save_student_project_mapping(mapping_file)
            if success:
                st.success(message)
                st.session_state.refresh_trigger += 1
            else:
                st.error(message)

        st.subheader("Admin Dashboard")
        stats = get_dashboard_stats()
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.markdown(f"<div class='metric-card'><b>Total Students</b><br>{stats['total_students']}</div>", unsafe_allow_html=True)
        with col2:
            st.markdown(f"<div class='metric-card'><b>Total Projects</b><br>{stats['total_projects']}</div>", unsafe_allow_html=True)
        with col3:
            st.markdown(f"<div class='metric-card'><b>Total Queries</b><br>{stats['total_queries']}</div>", unsafe_allow_html=True)
        with col4:
            rating = stats['avg_ratings'] if stats['avg_ratings'] is not None else "N/A"
            st.markdown(f"<div class='metric-card'><b>Avg Rating</b><br>{rating}</div>", unsafe_allow_html=True)

        st.subheader("Project-Wise Query Logs")
        with st.container():
            conn = sqlite3.connect(DB_PATH)
            query_logs = pd.read_sql_query("SELECT email, name, project_title, question, response, timestamp, feedback_rating FROM queries", conn)
            conn.close()
            if not query_logs.empty:
                project_titles = ["All Projects"] + sorted(query_logs['project_title'].unique().tolist())
                selected_project = st.selectbox("Filter by Project", project_titles)
                filtered_logs = query_logs if selected_project == "All Projects" else query_logs[query_logs['project_title'] == selected_project]
                for project_title in sorted(filtered_logs['project_title'].unique()):
                    st.markdown(f"<h3>Query Logs for Project: {project_title}</h3>", unsafe_allow_html=True)
                    project_logs = filtered_logs[filtered_logs['project_title'] == project_title]
                    with st.container():
                        for _, row in project_logs.iterrows():
                            rating = row['feedback_rating'] if pd.notna(row['feedback_rating']) else "Not rated"
                            initials = ''.join(word[0].upper() for word in row['name'].split()[:2])
                            st.markdown(
                                f"<div class='history-entry'><span class='avatar'>{initials}</span><strong>{row['name']} ({row['email']}) asked:</strong> {row['question']} <i>(at {format_timestamp(row['timestamp'])})</i><br><strong>Ky’ra replied:</strong> {row['response']}<br><strong>Rating:</strong> {rating}</div>",
                                unsafe_allow_html=True
                            )
                            st.markdown("---")
            else:
                st.markdown("<p style='font-family: \"Roboto\", sans-serif;'>No query logs available.</p>", unsafe_allow_html=True)

        st.subheader("Export Query Logs")
        with st.container():
            st.write("Select Date Range")
            date_range = st.date_input("Date Range", [datetime(2025, 6, 1), datetime(2025, 6, 1)], min_value=datetime(2020, 1, 1), max_value=datetime.now())
            if isinstance(date_range, tuple) and len(date_range) == 2:
                date_from, date_to = date_range
            else:
                date_from, date_to = date_range, date_range

            col1, col2 = st.columns(2)
            with col1:
                if st.button("Export ALL to CSV"):
                    csv_data = export_query_logs_to_csv(date_from=date_from, date_to=date_to)
                    if csv_data:
                        st.download_button(
                            label="Download All Query Logs CSV",
                            data=csv_data,
                            file_name=f"query_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}_all.csv",
                            mime="text/csv"
                        )
                    else:
                        st.warning("No query logs found for the selected criteria.")
            with col2:
                if st.button("Export ALL to PDF"):
                    pdf_data = export_query_logs_to_pdf(date_from=date_from, date_to=date_to)
                    if pdf_data:
                        st.download_button(
                            label="Download All Query Logs PDF",
                            data=pdf_data,
                            file_name=f"query_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}_all.pdf",
                            mime="application/pdf"
                        )
                    else:
                        st.warning("No query logs found for the selected criteria.")

        st.subheader("Export Student-Wise Query Logs")
        with st.container():
            conn = sqlite3.connect(DB_PATH)
            users = pd.read_sql_query("SELECT email, name FROM users WHERE role = 'student'", conn)
            conn.close()
            if not users.empty:
                student_email = st.selectbox("Select Student", users['email'].tolist(),
                                            format_func=lambda x: f"{x} ({users[users['email'] == x]['name'].iloc[0]})")
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Export Student CSV"):
                        csv_data = export_query_logs_to_csv(student_email=student_email, date_from=date_from, date_to=date_to)
                        if csv_data:
                            st.download_button(
                                label=f"Download {student_email} Query Logs CSV",
                                data=csv_data,
                                file_name=f"query_logs_{student_email}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                                mime="text/csv"
                            )
                        else:
                            st.warning("No query logs found for the selected student.")
                with col2:
                    if st.button("Export Student PDF"):
                        pdf_data = export_query_logs_to_pdf(student_email=student_email, date_from=date_from, date_to=date_to)
                        if pdf_data:
                            st.download_button(
                                label=f"Download {student_email} Query Logs PDF",
                                data=pdf_data,
                                file_name=f"query_logs_{student_email}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                                mime="application/pdf"
                            )
                        else:
                            st.warning("No query logs found for the selected student.")
            else:
                st.info("No student users found.")

        st.subheader("Export Project-Wise Query Logs")
        with st.container():
            projects = get_available_projects()
            if projects:
                project_filter = st.selectbox("Filter by Project", ["All Projects"] + projects)
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Export Project CSV"):
                        csv_data = export_query_logs_to_csv(project_filter=project_filter, date_from=date_from, date_to=date_to)
                        if csv_data:
                            filename = f"query_logs_{project_filter}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv" if project_filter != "All Projects" else f"query_logs_all_projects_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                            st.download_button(
                                label="Download Project CSV",
                                data=csv_data,
                                file_name=filename,
                                mime="text/csv"
                            )
                        else:
                            st.warning("No query logs found for the selected project.")
                with col2:
                    if st.button("Export Project PDF"):
                        pdf_data = export_query_logs_to_pdf(project_filter=project_filter, date_from=date_from, date_to=date_to)
                        if pdf_data:
                            filename = f"query_logs_{project_filter}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf" if project_filter != "All Projects" else f"query_logs_all_projects_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
                            st.download_button(
                                label="Download Project PDF",
                                data=pdf_data,
                                file_name=filename,
                                mime="application/pdf"
                            )
                        else:
                            st.warning("No query logs found for the selected project.")
            else:
                st.info("No projects found.")

    # Student-specific features
    if st.session_state.role == "student":
        st.markdown(f"<h1 style='text-align: center; color: #4fb8ac; font-family: \"Roboto\", sans-serif;'>👋 Welcome, {st.session_state.name}!</h1>", unsafe_allow_html=True)
        st.markdown("<p style='text-align: center; font-family: \"Roboto\", sans-serif;'>Ask Ky’ra about resumes, interviews, or projects!</p>", unsafe_allow_html=True)

        st.subheader("Your Project")
        project_title = get_project_title_for_student(st.session_state.email)
        if project_title:
            st.write(f"Assigned Project: **{project_title}**")
        else:
            st.info("No project assigned. Please contact the admin.")

        st.subheader("Submit Your Project Title")
        project_input = st.text_input("Enter your project title:", placeholder="E.g., AI-based Chatbot")
        if st.button("Submit Project"):
            if project_input:
                success, message = save_project(st.session_state.email, project_input)
                if success:
                    st.success(message)
                else:
                    st.error(message)
            else:
                st.error("Please enter a project title.")

        st.subheader("Export Your Query Logs")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Export to CSV"):
                csv_data = export_query_logs_to_csv(student_email=st.session_state.email)
                if csv_data:
                    st.download_button(
                        label="Download Your Query Logs CSV",
                        data=csv_data,
                        file_name=f"query_logs_{st.session_state.email}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                        mime="text/csv"
                    )
                else:
                    st.error("No query logs available.")
        with col2:
            if st.button("Export to PDF"):
                pdf_data = export_query_logs_to_pdf(student_email=st.session_state.email)
                if pdf_data:
                    st.download_button(
                        label="Download Your Query Logs PDF",
                        data=pdf_data,
                        file_name=f"query_logs_{st.session_state.email}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                        mime="application/pdf"
                    )
                else:
                    st.error("No query logs available.")

        st.subheader("Ask Ky’ra a Question")
        sample_questions = [
            "How do I write my internship resume?",
            "What are the best final-year projects in AI?",
            "How can I prepare for my interview?",
            "What skills for a cybersecurity career?"
        ]
        selected_question = st.selectbox("Choose a sample question or type your own:", sample_questions + ["Custom question..."])
        query_text = st.text_area("Your Question", value=selected_question if selected_question != "Custom question..." else "", height=150, placeholder="E.g., How to prepare for an internship interview?")

        st.markdown('<div class="submit-button">', unsafe_allow_html=True)
        if st.button("Submit", type="primary"):
            if not query_text:
                st.error("Please enter a query.")
            else:
                try:
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    project_title = get_project_title_for_student(st.session_state.email) or "No Project Assigned"
                    response = kyra_response(st.session_state.email, query_text, project_title)
                    save_query(st.session_state.email, st.session_state.name, project_title, query_text, response, timestamp)
                    st.session_state.chat_history.append({
                        "email": st.session_state.email,
                        "name": st.session_state.name,
                        "query": query_text,
                        "response": response,
                        "timestamp": timestamp
                    })
                    st.success("Query submitted successfully!")
                    with st.expander("🧠 Ky’ra’s Response", expanded=True):
                        initials = ''.join(word[0].upper() for word in st.session_state.name.split()[:2])
                        st.markdown(
                            f"<div class='history-entry'><span class='avatar'>{initials}</span><strong>Ky’ra’s Response:</strong> <br>{response}</br></div>",
                            unsafe_allow_html=True
                        )
                        feedback_rating = st.slider("Feedback Rating", min_value=1, max_value=5, value=3, step=1, key=f"rating_{timestamp}")
                        if st.button("Submit Rating", key=f"submit_rating_{timestamp}"):
                            if st.checkbox("Confirm rating submission", key=f"confirm_rating_{timestamp}"):
                                conn = sqlite3.connect(DB_PATH)
                                c = conn.cursor()
                                c.execute('UPDATE queries SET feedback_rating = ? WHERE email = ? AND timestamp = ?',
                                         (feedback_rating, st.session_state.email, timestamp))
                                conn.commit()
                                conn.close()
                                st.success("Feedback submitted!")
                            else:
                                st.warning("Please confirm the rating submission.")
                except Exception as e:
                    st.error(f"Failed to process query: {str(e)}")
        st.markdown('</div>', unsafe_allow_html=True)

        st.markdown("**🧾 Your Query History:**")
        try:
            conn = sqlite3.connect(DB_PATH)
            user_df = pd.read_sql_query("SELECT name, question, response, timestamp, feedback_rating FROM queries WHERE email = ? ORDER BY timestamp DESC",
                                       conn, params=(st.session_state.email,))
            conn.close()

            if not user_df.empty:
                user_df = user_df.drop_duplicates(subset=['question', 'timestamp'])
                items_per_page = 5
                total_pages = (len(user_df) + items_per_page - 1) // items_per_page
                st.session_state.page = max(1, min(st.session_state.page, total_pages))

                start_idx = (st.session_state.page - 1) * items_per_page
                end_idx = start_idx + items_per_page
                paginated_df = user_df.iloc[start_idx:end_idx]

                st.markdown('<div class="chat-container">', unsafe_allow_html=True)
                for _, row in paginated_df.iterrows():
                    response_text = row['response'] if pd.notna(row['response']) else "No response available."
                    rating = row['feedback_rating'] if pd.notna(row['feedback_rating']) else "Not rated"
                    initials = ''.join(word[0].upper() for word in row['name'].split()[:2])
                    with st.expander(f"Question at {format_timestamp(row['timestamp'])}"):
                        st.markdown(
                            f"<div class='history-entry'><span class='avatar'>{initials}</span><strong>You asked:</strong> {row['question']} <i>(at {format_timestamp(row['timestamp'])})</i><br><strong>Ky’ra replied:</strong> {response_text}<br><strong>Rating:</strong> {rating}</div>",
                            unsafe_allow_html=True
                        )
                        st.markdown("---")
                st.markdown('</div>', unsafe_allow_html=True)

                col1, col2, col3 = st.columns([1, 2, 1])
                with col1:
                    if st.button("Previous", disabled=st.session_state.page == 1):
                        st.session_state.page -= 1
                with col3:
                    if st.button("Next", disabled=st.session_state.page == total_pages):
                        st.session_state.page += 1
                with col2:
                    st.write(f"Page {st.session_state.page} of {total_pages}")
            else:
                st.markdown("<p style='font-family: \"Roboto\", sans-serif;'>No query history yet.</p>", unsafe_allow_html=True)
        except sqlite3.Error as e:
            logger.error(f"Database error in query history: {str(e)}")
            st.error("Failed to load query history.")

        st.subheader("Your Projects")
        user_projects = get_user_projects(st.session_state.email)
        if not user_projects.empty:
            st.markdown('<div class="chat-container">', unsafe_allow_html=True)
            for _, row in user_projects.iterrows():
                st.markdown(
                    f"<div class='history-entry'><strong>Project Title:</strong> {row['project_title']} <i>(at {format_timestamp(row['timestamp'])})</i></div>",
                    unsafe_allow_html=True
                )
                st.markdown("---")
            st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.markdown("<p style='font-family: \"Roboto\", sans-serif;'>No projects submitted.</p>", unsafe_allow_html=True)

# Footer
st.markdown(
    "<p class='chat-footer'>Ky’ra is here whenever you need. Ask freely. Grow boldly.</p>",
    unsafe_allow_html=True
)
st.markdown("Your query history and project submissions are securely stored.", unsafe_allow_html=True)