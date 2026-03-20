import os
import re
import uuid
import random
from datetime import date, datetime, timedelta
from flask import Flask, render_template, request, redirect, session, flash, jsonify, g
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from twilio.rest import Client
from db import dbconnect
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = "lawfirm_secret"

# -------- Twilio Setup --------
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# -------- Upload folder setup --------
UPLOAD_BASE      = os.path.join("static", "uploads")
UPLOAD_DOCS     = os.path.join(UPLOAD_BASE, "documents")
UPLOAD_EVIDENCE = os.path.join(UPLOAD_BASE, "evidence")
UPLOAD_PHOTOS   = os.path.join(UPLOAD_BASE, "photos")
for _folder in [UPLOAD_DOCS, UPLOAD_EVIDENCE, UPLOAD_PHOTOS]:
    os.makedirs(_folder, exist_ok=True)

ALLOWED_DOC  = {'pdf','doc','docx','xls','xlsx','txt','jpg','jpeg','png'}
ALLOWED_IMG  = {'jpg','jpeg','png','gif'}

def allowed(filename, allowed_set):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_set

def save_upload(file_obj, folder):
    """Save uploaded file with a UUID prefix; return stored filename or None."""
    if not file_obj or file_obj.filename == '':
        return None
    ext = secure_filename(file_obj.filename).rsplit('.', 1)[-1].lower()
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    file_obj.save(os.path.join(folder, unique_name))
    return unique_name

# -------- Phone number normalizer --------
def normalize_phone_from_form(data, field_name, default_code="+91"):
    """Accepts either full phone (+91...) or digits-only and returns normalized +<code><digits>."""
    raw = (data.get(field_name) or "").strip()
    if not raw:
        return ""

    # If already includes a + then just keep digits and prefix +
    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw)
        return f"+{digits}" if digits else ""

    # Otherwise use country code field if provided
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""

    code = (data.get(f"{field_name}_country_code") or default_code).strip() or default_code
    if not code.startswith("+"):
        code = "+" + re.sub(r"\D", "", code)

    return f"{code}{digits.lstrip('0')}"

# -------- DB connection helper (auto-close) --------
def get_db():
    """Get a DB connection, stored on Flask 'g' so it is reused and auto-closed."""
    if 'db' not in g:
        g.db = dbconnect()
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

# ----------------  HEALTH CHECK  ----------------
@app.route("/health")
def health():
    try:
        conn = dbconnect()
        conn.cursor().execute("SELECT 1")
        return jsonify({"status": "DB OK", "database": "law_firm"})
    except Exception as e:
        return jsonify({"status": "DB ERROR", "error": str(e)}), 500


# ----------------  CHECK PHONE IN DATABASE  ----------------
@app.route("/api/check_phone", methods=["POST"])
def check_phone():
    """Check if a phone number + email combination exists in the DB for a given role."""
    data = request.get_json()
    role   = data.get("role", "")
    email  = data.get("email", "")
    phone  = data.get("phone", "").strip()

    if not role or not email or not phone:
        return jsonify({"exists": False, "error": "Missing fields"})

    # Normalize incoming phone: extract digits, handle both +91... and standalone digits formats
    phone_digits = re.sub(r"\D", "", phone)  # Strip all non-digits
    
    print(f"\n=== PHONE CHECK DEBUG ===")
    print(f"Role: {role}, Email: {email}, Phone: {phone}")
    print(f"Phone digits: {phone_digits}")

    conn   = None
    cursor = None
    
    try:
        conn   = dbconnect()
        cursor = conn.cursor()
        
        if role == "admin":
            print(f"Executing admin query...")
            cursor.execute(
                "SELECT a.admin_id, ac.phone_number FROM admin a JOIN admin_contact ac ON a.admin_id = ac.admin_id WHERE a.email=%s",
                (email,)
            )
        elif role == "advocate":
            print(f"Executing advocate query...")
            cursor.execute(
                "SELECT a.advocate_id, ac.phone_number FROM advocate a JOIN advocate_contact ac ON a.advocate_id = ac.advocate_id WHERE a.email=%s",
                (email,)
            )
        elif role == "client":
            print(f"Executing client query...")
            cursor.execute(
                "SELECT client_id, phone_number FROM client WHERE email=%s",
                (email,)
            )
        else:
            return jsonify({"exists": False, "error": "Invalid role"})

        user = cursor.fetchone()
        if not user:
            print(f"No user found for {role} with email {email}")
            if cursor:
                cursor.close()
            if conn:
                conn.close()
            return jsonify({"exists": False})

        # Normalize database phone: extract digits and compare
        # Note: cursor returns dictionaries, so access by key name
        db_phone = user.get('phone_number', "") if user else ""
        db_phone_digits = re.sub(r"\D", "", db_phone)
        
        print(f"DB phone: {db_phone}")
        print(f"DB phone digits: {db_phone_digits}")
        print(f"Form phone digits: {phone_digits}")
        print(f"Match: {phone_digits == db_phone_digits}")
        print(f"=== END DEBUG ===\n")

        # Compare digits (handles both +91XXXXXXXXXX and XXXXXXXXXX formats)
        exists = (phone_digits == db_phone_digits)
        
        if cursor:
            cursor.close()
        if conn:
            conn.close()
            
        return jsonify({"exists": exists})
    except Exception as e:
        print(f"EXCEPTION in check_phone: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        if cursor:
            try:
                cursor.close()
            except:
                pass
        if conn:
            try:
                conn.close()
            except:
                pass
        return jsonify({"exists": False, "error": f"{type(e).__name__}: {str(e)}"})

@app.route("/")
def home():
    return render_template("home/index.html")

# ----------------  PASSWORD RESET (TWILIO) ----------------

@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        role = request.form.get("role")
        email = request.form.get("email")
        phone_number = request.form.get("phone_number")
        if not role or not email or not phone_number:
            flash("Role, email, and phone number are required.", "error")
            return redirect("/forgot_password")
        
        conn = dbconnect()
        cursor = conn.cursor()
        
        if role == "admin":
            cursor.execute("SELECT a.admin_id FROM admin a JOIN admin_contact ac ON a.admin_id = ac.admin_id WHERE a.email=%s AND ac.phone_number=%s", (email, phone_number))
        elif role == "advocate":
            cursor.execute("SELECT a.advocate_id FROM advocate a JOIN advocate_contact ac ON a.advocate_id = ac.advocate_id WHERE a.email=%s AND ac.phone_number=%s", (email, phone_number))
        elif role == "client":
            cursor.execute("SELECT client_id FROM client WHERE email=%s AND phone_number=%s", (email, phone_number))
        else:
            flash("Invalid role.", "error")
            return redirect("/forgot_password")
            
        user = cursor.fetchone()
        if not user:
            flash("No user found with the provided email and role.", "error")
            return redirect("/forgot_password")
            
        # Generate OTP
        otp = str(random.randint(100000, 999999))
        session["reset_otp"] = otp
        session["reset_otp_expiry"] = (datetime.now() + timedelta(minutes=10)).timestamp()
        session["reset_email"] = email
        session["reset_role"] = role
        
        # Send OTP via Twilio
        if TWILIO_AUTH_TOKEN == "[YOUR_AUTH_TOKEN_HERE]" or not TWILIO_AUTH_TOKEN:
            flash("Twilio Auth Token is not configured. Please check app.py", "error")
            return redirect("/forgot_password")
            
        # Normalize phone number to E.164 format
        phone_clean = phone_number.strip().replace(" ", "").replace("-", "")
        if not phone_clean.startswith("+"):
            phone_clean = "+" + phone_clean
            
        try:
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            message = client.messages.create(
                body=f"INS Firm: Your password reset OTP is {otp}. Valid for 10 minutes.",
                from_=TWILIO_PHONE_NUMBER,
                to=phone_clean
            )
            flash("OTP sent successfully via SMS.", "success")
            return redirect("/verify_otp")
        except Exception as e:
            err_msg = str(e)
            if "21408" in err_msg:
                flash("SMS sending failed: Your phone number's region is not enabled in Twilio. Please enable it at twilio.com/console → Messaging → Settings → Geo Permissions.", "error")
            else:
                flash(f"Failed to send SMS: {err_msg}", "error")
            return redirect("/forgot_password")
            
    return render_template("login/forgot_password.html")


@app.route("/verify_otp", methods=["GET", "POST"])
def verify_otp():
    if request.method == "POST":
        entered_otp = request.form.get("otp")
        stored_otp = session.get("reset_otp")
        expiry = session.get("reset_otp_expiry")
        
        if not stored_otp or not expiry:
            flash("No OTP request found. Please try again.", "error")
            return redirect("/forgot_password")
            
        if datetime.now().timestamp() > expiry:
            flash("OTP has expired. Please request a new one.", "error")
            session.pop("reset_otp", None)
            session.pop("reset_otp_expiry", None)
            return redirect("/forgot_password")
            
        if entered_otp == stored_otp:
            # OTP is correct, we map a flag so the reset route knows it's allowed
            session["otp_verified"] = True
            flash("OTP verified. Please set your new password.", "success")
            return redirect("/reset_password")
        else:
            flash("Invalid OTP. Please try again.", "error")
            
    return render_template("login/verify_otp.html")


@app.route("/reset_password", methods=["GET", "POST"])
def reset_password():
    if not session.get("otp_verified") or not session.get("reset_email") or not session.get("reset_role"):
        flash("You must verify an OTP before resetting your password.", "error")
        return redirect("/forgot_password")
        
    if request.method == "POST":
        new_password = request.form.get("new_password")
        confirm_password = request.form.get("confirm_password")
        
        if not new_password or not confirm_password:
            flash("Please fill in all fields.", "error")
            return redirect("/reset_password")
            
        if new_password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect("/reset_password")
            
        role = session.get("reset_role")
        email = session.get("reset_email")
        hashed_pw = generate_password_hash(new_password)
        
        conn = dbconnect()
        cursor = conn.cursor()
        try:
            if role == "admin":
                cursor.execute("UPDATE admin SET password_hash=%s WHERE email=%s", (hashed_pw, email))
            elif role == "advocate":
                cursor.execute("UPDATE advocate SET password_hash=%s WHERE email=%s", (hashed_pw, email))
            elif role == "client":
                cursor.execute("UPDATE client SET password_hash=%s WHERE email=%s", (hashed_pw, email))
            conn.commit()
            
            # Clear session keys
            session.pop("reset_otp", None)
            session.pop("reset_otp_expiry", None)
            session.pop("reset_email", None)
            session.pop("reset_role", None)
            session.pop("otp_verified", None)
            
            flash("Password updated successfully. You can now log in.", "success")
            return redirect("/login")
        except Exception as e:
            flash(f"Database error: {str(e)}", "error")
            return redirect("/reset_password")
            
    return render_template("login/reset_password.html")

# ----------------  UNIFIED LOGIN ----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        role     = request.form.get("role")
        email    = request.form.get("email")
        password = request.form.get("password")

        if not role:
            flash("Please select a role.", "error")
            return render_template("login/login.html")

        conn   = dbconnect()
        cursor = conn.cursor()

        # --- ADMIN ---
        if role == "admin":
            cursor.execute("SELECT * FROM admin WHERE email=%s", (email,))
            user = cursor.fetchone()
            if user and check_password_hash(user["password_hash"], password):
                full_name = f"{user['first_name']} {user.get('last_name','') or ''}".strip()
                session["user_name"] = full_name
                if user.get("role") == "SuperAdmin":
                    session["superadmin_id"] = user["admin_id"]
                    return redirect("/dashboard/superadmin")
                else:
                    session["admin_id"] = user["admin_id"]
                    return redirect("/dashboard/admin")
            else:
                flash("Invalid email or password.", "error")

        # --- ADVOCATE ---
        elif role == "advocate":
            cursor.execute("SELECT * FROM advocate WHERE email=%s", (email,))
            user = cursor.fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session["advocate_id"] = user["advocate_id"]
                session["user_name"] = f"{user['first_name']} {user.get('last_name','') or ''}".strip()
                return redirect("/dashboard/advocate")
            else:
                flash("Invalid email or password.", "error")

        # --- CLIENT ---
        elif role == "client":
            cursor.execute("SELECT * FROM client WHERE email=%s", (email,))
            user = cursor.fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session["client_id"] = user["client_id"]
                session["user_name"] = f"{user['first_name']} {user.get('last_name','') or ''}".strip()
                return redirect("/dashboard/client")
            else:
                flash("Invalid email or password.", "error")

        else:
            flash("Invalid role selected.", "error")

    return render_template("login/login.html")


# Keep legacy routes pointing to unified login
@app.route("/login/admin")
def admin_login():
    return redirect("/login")

@app.route("/login/advocate")
def advocate_login():
    return redirect("/login")

@app.route("/login/client")
def client_login():
    return redirect("/login")

@app.route("/login/superadmin")
def superadmin_login():
    return redirect("/login")


# ---------------- DASHBOARDS ----------------

@app.route("/dashboard/admin")
def admin_dashboard():
    if "admin_id" not in session and "superadmin_id" not in session:
        flash("Please log in first.", "error")
        return redirect("/login")
    return render_template("dashboard/admin_dashboard.html")


@app.route("/dashboard/advocate")
def advocate_dashboard():
    if "advocate_id" not in session:
        flash("Please log in first.", "error")
        return redirect("/login")
    return render_template("dashboard/advocate_dashboard.html")


@app.route("/dashboard/client")
def client_dashboard():
    if "client_id" not in session:
        flash("Please log in first.", "error")
        return redirect("/login")
    return render_template("dashboard/client_dashboard.html")



@app.route("/dashboard/superadmin")
def superadmin_dashboard():
    if "superadmin_id" not in session:
        flash("Please log in first.", "error")
        return redirect("/login")
    return render_template("dashboard/superadmin_dashboard.html")


# -------- AJAX: get cases for a client (used in review_files) --------
@app.route("/api/cases_for_client/<int:client_id>")
def api_cases_for_client(client_id):
    if "admin_id" not in session and "superadmin_id" not in session and "advocate_id" not in session:
        return jsonify([])
    conn = dbconnect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT case_id, case_number, case_title FROM cases WHERE client_id=%s AND case_status != 'Closed'",
        (client_id,)
    )
    return jsonify(cursor.fetchall())


# ------------- ADMIN PAGES -------------

@app.route("/admin/<page>", methods=["GET", "POST"])
def admin_pages(page):
    if "admin_id" not in session and "superadmin_id" not in session:
        flash("Unauthorized access. Please log in.", "error")
        return redirect("/login")

    conn = dbconnect()
    cursor = conn.cursor()

    # ---- POST handlers ----
    if request.method == "POST" and page == "review_files":
        case_id   = request.form.get("case_id")
        doc_title = request.form.get("doc_title")
        doc_type  = request.form.get("doc_type", "General")
        description = request.form.get("description", "")
        uploaded_by = session.get("user_name", "Admin")
        file_obj  = request.files.get("doc_file")
        filename  = save_upload(file_obj, UPLOAD_DOCS)
        if case_id and doc_title:
            try:
                cursor.execute(
                    "INSERT INTO document (case_id, document_title, document_type, file_path, "
                    "upload_date, uploaded_by, description) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (case_id, doc_title, doc_type,
                     filename or '', date.today(), uploaded_by, description)
                )
                conn.commit()
                flash("Document uploaded and saved successfully!", "success")
            except Exception as e:
                flash(f"Upload error: {str(e)}", "error")
        else:
            flash("Case and document title are required.", "error")
        return redirect(f"/admin/{page}")

    if request.method == "POST" and page == "review_evidence":
        case_id    = request.form.get("case_id")
        ev_title   = request.form.get("evidence_title")
        ev_type    = request.form.get("evidence_type")
        description = request.form.get("description", "")
        file_obj   = request.files.get("evidence_file")
        filename   = save_upload(file_obj, UPLOAD_EVIDENCE)
        if case_id and ev_title and ev_type:
            try:
                cursor.execute(
                    "INSERT INTO evidence (case_id, evidence_title, evidence_type, description, file_path) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (case_id, ev_title, ev_type, description, filename or '')
                )
                conn.commit()
                flash("Evidence submitted to registry successfully!", "success")
            except Exception as e:
                flash(f"Submission error: {str(e)}", "error")
        else:
            flash("Case, title and type are required.", "error")
        return redirect(f"/admin/{page}")

    if request.method == "POST" and page == "add_payment":
        case_id  = request.form.get("case_id")
        amount   = request.form.get("amount")
        mode     = request.form.get("payment_mode")
        status   = request.form.get("payment_status", "Completed")
        ref      = request.form.get("transaction_reference", "")
        pay_date = request.form.get("payment_date") or date.today()
        if case_id and amount and mode:
            try:
                cursor.execute(
                    "INSERT INTO payment (case_id, amount, payment_mode, payment_status, payment_date, transaction_reference) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (case_id, amount, mode, status, pay_date, ref)
                )
                conn.commit()
                flash("Payment recorded successfully!", "success")
            except Exception as e:
                flash(f"Payment error: {str(e)}", "error")
        else:
            flash("Case, amount and mode are required.", "error")
        return redirect("/admin/add_payment")

    if request.method == "POST":
        if page == "create_advocate":
            first_name     = request.form.get("first_name")
            last_name      = request.form.get("last_name")
            specialization = request.form.get("specialization", "General")
            email          = request.form.get("email")
            password       = request.form.get("password")
            try:
                hashed_pw = generate_password_hash(password)
                phone_code    = request.form.get("phone_country_code", "+91").strip()
                raw_phone     = request.form.get("phone_number", "").strip()
                raw_phone_alt = request.form.get("phone_number_alt", "").strip()

                # Normalize phone numbers to include country code (e.g., +91XXXXXXXXXX)
                digits_only = re.sub(r"\D", "", raw_phone)
                phone = digits_only
                if digits_only and not digits_only.startswith("+"):
                    phone = f"{phone_code}{digits_only.lstrip('0')}"

                digits_alt = re.sub(r"\D", "", raw_phone_alt)
                phone_alt = digits_alt
                if digits_alt and not digits_alt.startswith("+"):
                    phone_alt = f"{phone_code}{digits_alt.lstrip('0')}"

                # Insert into main table
                cursor.execute(
                    "INSERT INTO advocate (first_name, last_name, specialization, email, password_hash) VALUES (%s,%s,%s,%s,%s)",
                    (first_name, last_name, specialization, email, hashed_pw)
                )
                advocate_id = cursor.lastrowid
                
                # Insert into contact table
                if phone:
                    cursor.execute("INSERT INTO advocate_contact (advocate_id, phone_number, phone_type) VALUES (%s, %s, 'Mobile')", (advocate_id, phone))
                if phone_alt:
                    cursor.execute("INSERT INTO advocate_contact (advocate_id, phone_number, phone_type) VALUES (%s, %s, 'Alternate')", (advocate_id, phone_alt))
                
                # Auto-assign to Admin
                admin_id = session.get("admin_id") or session.get("superadmin_id")
                if admin_id:
                    cursor.execute(
                        "INSERT INTO admin_advocate_manage (admin_id, advocate_id) VALUES (%s, %s)",
                        (admin_id, advocate_id)
                    )
                
                conn.commit()
                flash("Advocate created successfully!", "success")
            except Exception as e:
                flash(f"Error: {str(e)}", "error")
            return redirect(f"/admin/{page}")

        elif page == "create_client":
            first_name  = request.form.get("first_name")
            last_name   = request.form.get("last_name")
            email       = request.form.get("email")
            phone_code  = request.form.get("phone_country_code", "+91").strip()
            raw_phone   = request.form.get("phone_number", "").strip()
            address     = request.form.get("address")
            password    = request.form.get("password")
            add_case    = request.form.get("add_case")

            # Normalize phone to include country code (e.g., +91XXXXXXXXXX)
            digits_only = re.sub(r"\D", "", raw_phone)
            phone = digits_only
            if digits_only and not digits_only.startswith("+"):
                phone = f"{phone_code}{digits_only.lstrip('0')}"

            try:
                hashed_pw = generate_password_hash(password)
                cursor.execute(
                    "INSERT INTO client (first_name, last_name, email, phone_number, address, password_hash) VALUES (%s,%s,%s,%s,%s,%s)",
                    (first_name, last_name, email, phone, address, hashed_pw)
                )
                client_id = cursor.lastrowid
                
                if add_case == "yes":
                    case_number = request.form.get("case_number")
                    case_title  = request.form.get("case_title")
                    case_type   = request.form.get("case_type")
                    court_id    = request.form.get("court_id")
                    description = request.form.get("description")
                    cursor.execute(
                        "INSERT INTO cases (client_id, case_number, case_title, case_type, court_id, case_status, description) VALUES (%s,%s,%s,%s,%s,'Open',%s)",
                        (client_id, case_number, case_title, case_type, court_id, description)
                    )
                
                conn.commit()
                flash("Client created successfully!", "success")
            except Exception as e:
                flash(f"Error: {str(e)}", "error")
            return redirect(f"/admin/{page}")

        elif page == "create_case":
            client_id   = request.form.get("client_id")
            case_title  = request.form.get("case_title")
            case_type   = request.form.get("case_type")
            court_id    = request.form.get("court_id")
            case_number = request.form.get("case_number", f"LC-{random.randint(1000, 9999)}")
            description = request.form.get("description", "")
            if client_id and case_title and case_type and court_id:
                try:
                    cursor.execute(
                        "INSERT INTO cases (client_id, case_number, case_title, case_type, court_id, case_status, description) "
                        "VALUES (%s,%s,%s,%s,%s,'Pending',%s)",
                        (client_id, case_number, case_title, case_type, court_id, description)
                    )
                    conn.commit()
                    flash(f"Case {case_number} created successfully!", "success")
                except Exception as e:
                    flash(f"Error creating case: {str(e)}", "error")
            else:
                flash("Client, case number, title and type are required.", "error")
            return redirect("/admin/create_case")

        elif page == "close_case":
            case_id     = request.form.get("case_id")
            disposition = request.form.get("disposition", "Closed")
            close_date  = request.form.get("close_date") or date.today()
            remarks     = request.form.get("final_remarks", "")
            if case_id:
                try:
                    cursor.execute(
                        "UPDATE cases SET case_status=%s, closing_date=%s, description=CONCAT(IFNULL(description,''), '\n[Closed] ', %s) WHERE case_id=%s",
                        (disposition, close_date, remarks, case_id)
                    )
                    conn.commit()
                    flash("Case closed successfully!", "success")
                except Exception as e:
                    flash(f"Error closing case: {str(e)}", "error")
            else:
                flash("Please select a case to close.", "error")
            return redirect("/admin/close_case")

        elif page == "reopen":
            case_id      = request.form.get("case_id")
            reopen_date  = request.form.get("reopen_date") or date.today()
            reason       = request.form.get("reopen_reason", "")
            remarks      = request.form.get("reopen_remarks", "")
            if case_id:
                try:
                    cursor.execute(
                        "UPDATE cases SET case_status='Open', closing_date=NULL, description=CONCAT(IFNULL(description,''), '\n[Reopened] ', %s, ' - ', %s) WHERE case_id=%s",
                        (reason, remarks, case_id)
                    )
                    conn.commit()
                    flash("Case reopened successfully!", "success")
                except Exception as e:
                    flash(f"Error reopening case: {str(e)}", "error")
            else:
                flash("Please select a case to reopen.", "error")
            return redirect("/admin/reopen")

        elif page == "assign_case":
            case_id     = request.form.get("case_id")
            advocate_id = request.form.get("advocate_id")
            role_label  = request.form.get("role", "Lead Counsel")
            try:
                cursor.execute(
                    "INSERT INTO advocate_case (advocate_id, case_id, role) VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE role=%s",
                    (advocate_id, case_id, role_label, role_label)
                )
                # Also link the client to this advocate so they see the client in their dashboard
                cursor.execute("SELECT client_id FROM cases WHERE case_id=%s", (case_id,))
                case_row = cursor.fetchone()
                if case_row:
                    cursor.execute(
                        "INSERT IGNORE INTO advocate_client (advocate_id, client_id) VALUES (%s,%s)",
                        (advocate_id, case_row["client_id"])
                    )
                conn.commit()
                flash("Case assigned to advocate successfully!", "success")
            except Exception as e:
                flash(f"Error assigning case: {str(e)}", "error")
            return redirect(f"/admin/{page}")

    # ---- GET handlers ----
    page_filename = "Financial Reports" if page == "financial_reports" else page

    # Shared folder check first
    if os.path.exists(f"templates/buttons/shared/{page_filename}.html"):
        extra = {}
        if page == "directory_view":
            cursor.execute("SELECT court_name, court_type, city, state FROM court ORDER BY court_name")
            extra["courts"] = cursor.fetchall()
        elif page == "view_client":
            cursor.execute("SELECT client_id, first_name, last_name, email, phone_number, address FROM client ORDER BY client_id DESC")
            extra["clients"] = cursor.fetchall()
        elif page == "create_case":
            cursor.execute("SELECT client_id, first_name, last_name, email FROM client ORDER BY first_name")
            extra["clients"] = cursor.fetchall()
            cursor.execute("SELECT court_id, court_name, city FROM court ORDER BY court_name")
            extra["courts"] = cursor.fetchall()
            cursor.execute(
                "SELECT c.case_id, c.case_number, c.case_title, c.case_type, c.case_status, cl.first_name, cl.last_name "
                "FROM cases c JOIN client cl ON c.client_id = cl.client_id ORDER BY c.filing_date DESC LIMIT 20"
            )
            extra["recent_cases"] = cursor.fetchall()
        elif page == "financial_reports":
            cursor.execute(
                "SELECT p.payment_id, p.payment_date, p.amount, p.payment_mode, p.payment_status, "
                "c.case_number, cl.first_name, cl.last_name "
                "FROM payment p JOIN cases c ON p.case_id = c.case_id "
                "JOIN client cl ON c.client_id = cl.client_id "
                "ORDER BY p.payment_date DESC"
            )
            extra["payments"] = cursor.fetchall()
            cursor.execute("SELECT COALESCE(SUM(amount),0) AS total FROM payment WHERE payment_status='Completed'")
            extra["total_revenue"] = cursor.fetchone()["total"]
            cursor.execute("SELECT COUNT(*) AS cnt FROM payment WHERE payment_status !='Completed'")
            extra["pending_count"] = cursor.fetchone()["cnt"]
            cursor.execute("SELECT COALESCE(SUM(amount),0) AS total FROM payment WHERE payment_status='Completed' AND MONTH(payment_date)=MONTH(CURDATE()) AND YEAR(payment_date)=YEAR(CURDATE())")
            extra["monthly_collected"] = cursor.fetchone()["total"]
        elif page == "close_case":
            cursor.execute(
                "SELECT c.case_id, c.case_number, c.case_title, cl.first_name, cl.last_name "
                "FROM cases c JOIN client cl ON c.client_id = cl.client_id "
                "WHERE c.case_status NOT IN ('Closed','Dismissed','Settled') ORDER BY c.filing_date DESC"
            )
            extra["cases"] = cursor.fetchall()
        elif page == "reopen":
            cursor.execute(
                "SELECT c.case_id, c.case_number, c.case_title, c.closing_date, c.case_status, cl.first_name, cl.last_name "
                "FROM cases c JOIN client cl ON c.client_id = cl.client_id "
                "WHERE c.case_status IN ('Closed','Dismissed','Settled') ORDER BY c.closing_date DESC"
            )
            extra["closed_cases"] = cursor.fetchall()
        elif page == "legal_sections":
            cursor.execute(
                "SELECT s.section_id, s.section_no, s.section_title, s.section_description, l.law_name "
                "FROM section s JOIN law l ON s.law_id = l.law_id ORDER BY s.section_no"
            )
            extra["sections"] = cursor.fetchall()

        return render_template(
            f"buttons/shared/{page_filename}.html",
            back_url="/dashboard/admin",
            form_action=f"/admin/{page}",
            **extra
        )

    if not os.path.exists(f"templates/buttons/admin/{page_filename}.html"):
        flash(f"Page '{page_filename}' is under construction.", "error")
        return redirect("/dashboard/admin")

    # Role-specific data
    if page == "view_client":
        # Admin sees only clients whose advocates are managed by this admin
        admin_id = session.get("admin_id") or session.get("superadmin_id")
        if session.get("superadmin_id"):
            cursor.execute("SELECT client_id, first_name, last_name, email, phone_number, address FROM client ORDER BY client_id DESC")
        else:
            cursor.execute("""
                SELECT DISTINCT c.client_id, c.first_name, c.last_name, c.email, c.phone_number, c.address
                FROM client c
                JOIN cases cs ON cs.client_id = c.client_id
                JOIN advocate_case ac ON ac.case_id = cs.case_id
                JOIN admin_advocate_manage m ON m.advocate_id = ac.advocate_id
                WHERE m.admin_id = %s
                ORDER BY c.client_id DESC
            """, (admin_id,))
        return render_template(f"buttons/admin/{page_filename}.html", clients=cursor.fetchall())

    if page == "assign_case":
        cursor.execute(
            "SELECT c.case_id, c.case_number, c.case_title, c.case_type, c.case_status, cl.first_name, cl.last_name "
            "FROM cases c JOIN client cl ON c.client_id = cl.client_id "
            "ORDER BY c.filing_date DESC"
        )
        cases = cursor.fetchall()
        cursor.execute("SELECT advocate_id, first_name, last_name, specialization FROM advocate WHERE is_active=1 ORDER BY first_name")
        advocates = cursor.fetchall()
        return render_template(f"buttons/admin/{page_filename}.html", cases=cases, advocates=advocates,
                               back_url="/dashboard/admin", form_action=f"/admin/{page}")

    if page == "review_files":
        cursor.execute("SELECT client_id, first_name, last_name, email FROM client ORDER BY first_name")
        clients = cursor.fetchall()
        cursor.execute(
            "SELECT d.document_id, d.document_title, d.document_type, d.file_path, "
            "d.upload_date, d.uploaded_by, d.description, c.case_number, "
            "cl.first_name, cl.last_name "
            "FROM document d "
            "LEFT JOIN cases c ON d.case_id = c.case_id "
            "LEFT JOIN client cl ON d.client_id = cl.client_id OR c.client_id = cl.client_id "
            "ORDER BY d.created_at DESC"
        )
        documents = cursor.fetchall()
        return render_template(
            f"buttons/admin/{page_filename}.html",
            clients=clients, documents=documents,
            back_url="/dashboard/admin", form_action=f"/admin/{page}"
        )

    if page == "review_evidence":
        cursor.execute(
            "SELECT e.evidence_id, e.evidence_title, e.evidence_type, e.description, "
            "e.file_path, e.created_at, c.case_number "
            "FROM evidence e JOIN cases c ON e.case_id = c.case_id ORDER BY e.created_at DESC"
        )
        evidences = cursor.fetchall()
        cursor.execute(
            "SELECT c.case_id, c.case_number, c.case_title FROM cases c "
            "WHERE c.case_status != 'Closed' ORDER BY c.filing_date DESC"
        )
        cases = cursor.fetchall()
        return render_template(
            f"buttons/admin/{page_filename}.html",
            evidences=evidences, cases=cases,
            back_url="/dashboard/admin", form_action=f"/admin/{page}"
        )

    if page == "view_advocates":
        # Admin sees only advocates assigned to them
        admin_id = session.get("admin_id") or session.get("superadmin_id")
        if session.get("superadmin_id"):
            cursor.execute("SELECT advocate_id, first_name, last_name, email, specialization, is_active FROM advocate ORDER BY advocate_id DESC")
        else:
            cursor.execute("""
                SELECT a.advocate_id, a.first_name, a.last_name, a.email, a.specialization, a.is_active
                FROM advocate a
                JOIN admin_advocate_manage m ON a.advocate_id = m.advocate_id
                WHERE m.admin_id = %s
                ORDER BY a.advocate_id DESC
            """, (admin_id,))
        return render_template("buttons/admin/view_advocates.html", advocates=cursor.fetchall())

    if page == "view_cases":
        cursor.execute(
            "SELECT c.case_id, c.case_number, c.case_title, c.case_type, c.case_status, "
            "c.description, ct.court_name, cl.first_name, cl.last_name "
            "FROM cases c JOIN client cl ON c.client_id = cl.client_id "
            "LEFT JOIN court ct ON c.court_id = ct.court_id "
            "ORDER BY c.filing_date DESC"
        )
        cases = cursor.fetchall()
        cursor.execute("SELECT court_id, court_name, city, state FROM court ORDER BY court_name")
        courts = cursor.fetchall()
        return render_template("buttons/admin/view_cases.html", cases=cases, courts=courts,
                               back_url="/dashboard/admin")

    if page == "view_assignments":
        # Admin sees advocate->client assignments for their managed advocates
        admin_id = session.get("admin_id") or session.get("superadmin_id")
        cursor.execute("""
            SELECT ac.advocate_id, ac.client_id,
                   a.first_name AS adv_first, a.last_name AS adv_last, a.specialization,
                   c.first_name AS client_first, c.last_name AS client_last,
                   c.email AS client_email, c.phone_number AS client_phone
            FROM advocate_client ac
            JOIN advocate a ON ac.advocate_id = a.advocate_id
            JOIN client c ON ac.client_id = c.client_id
            JOIN admin_advocate_manage m ON m.advocate_id = ac.advocate_id
            WHERE m.admin_id = %s
            ORDER BY a.first_name, c.first_name
        """, (admin_id,))
        advocate_client_assignments = cursor.fetchall()
        return render_template("buttons/admin/view_assignments.html",
                               advocate_client_assignments=advocate_client_assignments)

    if page == "create_case":
        cursor.execute("SELECT client_id, first_name, last_name, email FROM client ORDER BY first_name")
        clients = cursor.fetchall()
        cursor.execute(
            "SELECT c.case_id, c.case_number, c.case_title, c.case_type, c.case_status, cl.first_name, cl.last_name "
            "FROM cases c JOIN client cl ON c.client_id = cl.client_id ORDER BY c.filing_date DESC LIMIT 20"
        )
        recent_cases = cursor.fetchall()
        return render_template(
            "buttons/shared/create_case.html",
            clients=clients, recent_cases=recent_cases,
            back_url="/dashboard/admin", form_action="/admin/create_case"
        )

    if page in ["add_payment", "update_hearing", "update_verdict"]:
        # Admin sees only cases for their managed advocates
        admin_id = session.get("admin_id")
        cursor.execute("""
            SELECT DISTINCT cs.case_id, cs.case_number, cs.case_title, cl.first_name, cl.last_name
            FROM cases cs
            JOIN client cl ON cs.client_id = cl.client_id
            JOIN advocate_case ac ON ac.case_id = cs.case_id
            JOIN admin_advocate_manage m ON m.advocate_id = ac.advocate_id
            WHERE m.admin_id = %s
            ORDER BY cs.case_number
        """, (admin_id,))
        cases = cursor.fetchall()

        if page == "add_payment":
            cursor.execute("""
                SELECT p.payment_id, p.payment_date, p.amount, p.payment_mode,
                       p.payment_status, p.transaction_reference, cs.case_number,
                       cl.first_name, cl.last_name
                FROM payment p
                JOIN cases cs ON p.case_id = cs.case_id
                JOIN client cl ON cs.client_id = cl.client_id
                JOIN advocate_case ac ON ac.case_id = cs.case_id
                JOIN admin_advocate_manage m ON m.advocate_id = ac.advocate_id
                WHERE m.admin_id = %s
                ORDER BY p.payment_date DESC LIMIT 50
            """, (admin_id,))
            payments = cursor.fetchall()
            from datetime import date as _date
            return render_template(
                "buttons/shared/add_payment.html",
                cases=cases, payments=payments,
                back_url="/dashboard/admin", form_action="/admin/add_payment",
                today=_date.today()
            )
        elif page == "update_hearing":
            cursor.execute("""
                SELECT h.*, cs.case_number
                FROM hearing h
                JOIN cases cs ON h.case_id = cs.case_id
                JOIN advocate_case ac ON ac.case_id = cs.case_id
                JOIN admin_advocate_manage m ON m.advocate_id = ac.advocate_id
                WHERE m.admin_id = %s
                ORDER BY h.hearing_date DESC, h.hearing_time DESC LIMIT 50
            """, (admin_id,))
            hearings = cursor.fetchall()
            from datetime import date as _date
            return render_template(
                "buttons/shared/update_hearing.html",
                cases=cases, hearings=hearings,
                back_url="/dashboard/admin", form_action="/admin/update_hearing",
                today=_date.today()
            )
        elif page == "update_verdict":
            cursor.execute("""
                SELECT v.*, cs.case_number
                FROM verdict v
                JOIN cases cs ON v.case_id = cs.case_id
                JOIN advocate_case ac ON ac.case_id = cs.case_id
                JOIN admin_advocate_manage m ON m.advocate_id = ac.advocate_id
                WHERE m.admin_id = %s
                ORDER BY v.verdict_date DESC LIMIT 50
            """, (admin_id,))
            verdicts = cursor.fetchall()
            from datetime import date as _date
            return render_template(
                "buttons/shared/update_verdict.html",
                cases=cases, verdicts=verdicts,
                back_url="/dashboard/admin", form_action="/admin/update_verdict",
                today=_date.today()
            )

    return render_template(f"buttons/admin/{page_filename}.html")


# ------------- SUPERADMIN PAGES -------------

@app.route("/superadmin/<page>", methods=["GET", "POST"])
def superadmin_pages(page):
    if "superadmin_id" not in session:
        flash("Unauthorized access. Please log in.", "error")
        return redirect("/login")

    conn = dbconnect()
    cursor = conn.cursor()

    if request.method == "POST" and page == "add_payment":
        case_id  = request.form.get("case_id")
        amount   = request.form.get("amount")
        mode     = request.form.get("payment_mode")
        status   = request.form.get("payment_status", "Completed")
        ref      = request.form.get("transaction_reference", "")
        pay_date = request.form.get("payment_date") or date.today()
        if case_id and amount and mode:
            try:
                cursor.execute(
                    "INSERT INTO payment (case_id, amount, payment_mode, payment_status, payment_date, transaction_reference) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (case_id, amount, mode, status, pay_date, ref)
                )
                conn.commit()
                flash("Payment recorded successfully!", "success")
            except Exception as e:
                flash(f"Payment error: {str(e)}", "error")
        else:
            flash("Case, amount and mode are required.", "error")
        return redirect("/superadmin/add_payment")

    if request.method == "POST" and page == "update_hearing":
        case_id = request.form.get("case_id")
        hearing_no = request.form.get("hearing_no")
        hearing_date = request.form.get("hearing_date")
        hearing_time = request.form.get("hearing_time") or None
        hearing_status = request.form.get("hearing_status")
        remarks = request.form.get("remarks", "")
        if case_id and hearing_no and hearing_date and hearing_status:
            try:
                cursor.execute(
                    "INSERT INTO hearing (case_id, hearing_no, hearing_date, hearing_time, hearing_status, remarks) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (case_id, hearing_no, hearing_date, hearing_time, hearing_status, remarks)
                )
                conn.commit()
                flash("Hearing details recorded successfully!", "success")
            except Exception as e:
                flash(f"Error recording hearing: {str(e)}", "error")
        else:
            flash("Required fields are missing.", "error")
        return redirect("/superadmin/update_hearing")

    if request.method == "POST" and page == "update_verdict":
        case_id = request.form.get("case_id")
        verdict_date = request.form.get("verdict_date")
        result = request.form.get("verdict_result")
        summary = request.form.get("judgement_summary")
        fine = request.form.get("fine_amount") or None
        imprisonment = request.form.get("imprisonment_years") or None
        if case_id and verdict_date and result and summary:
            try:
                cursor.execute(
                    "INSERT INTO verdict (case_id, verdict_date, verdict_result, judgement_summary, fine_amount, imprisonment_years) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (case_id, verdict_date, result, summary, fine, imprisonment)
                )
                conn.commit()
                flash("Verdict recorded successfully!", "success")
            except Exception as e:
                flash(f"Error recording verdict: {str(e)}", "error")
        else:
            flash("Required fields are missing.", "error")
        return redirect("/superadmin/update_verdict")

    if request.method == "POST":
        if page == "manage_advocate":
            first_name     = request.form.get("first_name")
            last_name      = request.form.get("last_name")
            specialization = request.form.get("specialization", "General")
            email          = request.form.get("email")
            password       = request.form.get("password")
            phone          = normalize_phone_from_form(request.form, "phone_number")
            phone_alt      = normalize_phone_from_form(request.form, "phone_number_alt")
            if first_name and email and password:
                try:
                    hashed_pw = generate_password_hash(password)
                    
                    # 1. Insert into main advocate table
                    cursor.execute(
                        "INSERT INTO advocate (first_name, last_name, specialization, email, password_hash) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (first_name, last_name, specialization, email, hashed_pw)
                    )
                    advocate_id = cursor.lastrowid
                    
                    # 2. Insert into advocate_contact table
                    if phone:
                        cursor.execute("INSERT INTO advocate_contact (advocate_id, phone_number, phone_type) VALUES (%s, %s, 'Mobile')", (advocate_id, phone))
                    if phone_alt:
                        cursor.execute("INSERT INTO advocate_contact (advocate_id, phone_number, phone_type) VALUES (%s, %s, 'Alternate')", (advocate_id, phone_alt))

                    # Save profile photo
                    photo = request.files.get("advocate_photo")
                    photo_name = save_upload(photo, UPLOAD_PHOTOS)
                    if photo_name:
                        cursor.execute(
                            "INSERT INTO advocate_profile (advocate_id, photo_url) VALUES (%s, %s)",
                            (advocate_id, photo_name)
                        )
                        
                    # Link to this superadmin
                    admin_id = session.get("superadmin_id")
                    if admin_id:
                        cursor.execute(
                            "INSERT INTO admin_advocate_manage (admin_id, advocate_id) VALUES (%s, %s)",
                            (admin_id, advocate_id)
                        )
                    conn.commit()
                    flash("Advocate registered successfully!", "success")
                except Exception as e:
                    flash(f"Error: {str(e)}", "error")
            else:
                flash("First name, email and password are required.", "error")
            return redirect("/superadmin/manage_advocate")

        # -------- Review Files POST (superadmin) --------
        if page == "review_files":
            case_id   = request.form.get("case_id")
            doc_title = request.form.get("doc_title")
            doc_type  = request.form.get("doc_type", "General")
            description = request.form.get("description", "")
            uploaded_by = session.get("user_name", "SuperAdmin")
            file_obj  = request.files.get("doc_file")
            filename  = save_upload(file_obj, UPLOAD_DOCS)
            if case_id and doc_title:
                try:
                    cursor.execute(
                        "INSERT INTO document (case_id, document_title, document_type, file_path, "
                        "upload_date, uploaded_by, description) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (case_id, doc_title, doc_type, filename or '', date.today(), uploaded_by, description)
                    )
                    conn.commit()
                    flash("Document uploaded successfully!", "success")
                except Exception as e:
                    flash(f"Upload error: {str(e)}", "error")
            else:
                flash("Case and document title are required.", "error")
            return redirect("/superadmin/review_files")

        # -------- Review Evidence POST (superadmin) --------
        if page == "review_evidence":
            case_id    = request.form.get("case_id")
            ev_title   = request.form.get("evidence_title")
            ev_type    = request.form.get("evidence_type")
            description = request.form.get("description", "")
            file_obj   = request.files.get("evidence_file")
            filename   = save_upload(file_obj, UPLOAD_EVIDENCE)
            if case_id and ev_title and ev_type:
                try:
                    cursor.execute(
                        "INSERT INTO evidence (case_id, evidence_title, evidence_type, description, file_path) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (case_id, ev_title, ev_type, description, filename or '')
                    )
                    conn.commit()
                    flash("Evidence submitted successfully!", "success")
                except Exception as e:
                    flash(f"Submission error: {str(e)}", "error")
            else:
                flash("Case, title and type are required.", "error")
            return redirect("/superadmin/review_evidence")

        # -------- Assign Case POST (superadmin) --------
        if page == "assign_case":
            case_id     = request.form.get("case_id")
            advocate_id = request.form.get("advocate_id")
            role_label  = request.form.get("role", "Lead Counsel")
            try:
                cursor.execute(
                    "INSERT INTO advocate_case (advocate_id, case_id, role) VALUES (%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE role=%s",
                    (advocate_id, case_id, role_label, role_label)
                )
                # Also link the client to this advocate so they see the client in their dashboard
                cursor.execute("SELECT client_id FROM cases WHERE case_id=%s", (case_id,))
                case_row = cursor.fetchone()
                if case_row:
                    cursor.execute(
                        "INSERT IGNORE INTO advocate_client (advocate_id, client_id) VALUES (%s,%s)",
                        (advocate_id, case_row["client_id"])
                    )
                conn.commit()
                flash("Case assigned to advocate successfully!", "success")
            except Exception as e:
                flash(f"Error assigning case: {str(e)}", "error")
            return redirect("/superadmin/assign_case")

        # -------- Create Case POST (superadmin) --------
        if page == "create_case":
            client_id   = request.form.get("client_id")
            case_number = request.form.get("case_number")
            case_title  = request.form.get("case_title")
            case_type   = request.form.get("case_type")
            description = request.form.get("description", "")
            filing_date = request.form.get("filing_date") or date.today()
            if client_id and case_number and case_title and case_type:
                try:
                    cursor.execute(
                        "INSERT INTO cases (client_id, case_number, case_title, case_type, case_status, description, filing_date) "
                        "VALUES (%s,%s,%s,%s,'Open',%s,%s)",
                        (client_id, case_number, case_title, case_type, description, filing_date)
                    )
                    conn.commit()
                    flash("Case created successfully!", "success")
                except Exception as e:
                    flash(f"Error creating case: {str(e)}", "error")
            else:
                flash("Client, case number, title and type are required.", "error")
            return redirect("/superadmin/create_case")

        # -------- Close Case POST (superadmin) --------
        if page == "close_case":
            case_id     = request.form.get("case_id")
            disposition = request.form.get("disposition", "Closed")
            close_date  = request.form.get("close_date") or date.today()
            remarks     = request.form.get("final_remarks", "")
            if case_id:
                try:
                    cursor.execute(
                        "UPDATE cases SET case_status=%s, closing_date=%s, description=CONCAT(IFNULL(description,''), '\n[Closed] ', %s) WHERE case_id=%s",
                        (disposition, close_date, remarks, case_id)
                    )
                    conn.commit()
                    flash("Case closed successfully!", "success")
                except Exception as e:
                    flash(f"Error closing case: {str(e)}", "error")
            else:
                flash("Please select a case to close.", "error")
            return redirect("/superadmin/close_case")

        # -------- Reopen Case POST (superadmin) --------
        if page == "reopen":
            case_id      = request.form.get("case_id")
            reopen_date  = request.form.get("reopen_date") or date.today()
            reason       = request.form.get("reopen_reason", "")
            remarks      = request.form.get("reopen_remarks", "")
            if case_id:
                try:
                    cursor.execute(
                        "UPDATE cases SET case_status='Open', closing_date=NULL, description=CONCAT(IFNULL(description,''), '\n[Reopened] ', %s, ' - ', %s) WHERE case_id=%s",
                        (reason, remarks, case_id)
                    )
                    conn.commit()
                    flash("Case reopened successfully!", "success")
                except Exception as e:
                    flash(f"Error reopening case: {str(e)}", "error")
            else:
                flash("Please select a case to reopen.", "error")
            return redirect("/superadmin/reopen")

        if page == "manage_admin":
            action = request.form.get("action", "create_admin")

            # ---- Assign Advocate to Admin ----
            if action == "assign_advocate":
                adm_id = request.form.get("admin_id")
                adv_id = request.form.get("advocate_id")
                if adm_id and adv_id:
                    try:
                        cursor.execute(
                            "INSERT INTO admin_advocate_manage (admin_id, advocate_id) VALUES (%s,%s) "
                            "ON DUPLICATE KEY UPDATE admin_id=admin_id",
                            (adm_id, adv_id)
                        )
                        conn.commit()
                        flash("Advocate assigned to Admin successfully!", "success")
                    except Exception as e:
                        flash(f"Error assigning advocate: {str(e)}", "error")
                else:
                    flash("Please select both an admin and an advocate.", "error")
                return redirect("/superadmin/manage_admin")

            # ---- Create Admin ----
            first_name  = request.form.get("first_name")
            last_name   = request.form.get("last_name")
            email       = request.form.get("email")
            password    = request.form.get("password")
            phone       = normalize_phone_from_form(request.form, "phone_number")
            phone_alt   = normalize_phone_from_form(request.form, "phone_number_alt")
            if first_name and email and password:
                try:
                    hashed_pw = generate_password_hash(password)
                    cursor.execute(
                        "INSERT INTO admin (first_name, last_name, email, password_hash, role) VALUES (%s,%s,%s,%s,'Admin')",
                        (first_name, last_name, email, hashed_pw)
                    )
                    admin_id = cursor.lastrowid
                    if phone:
                        cursor.execute("INSERT INTO admin_contact (admin_id, phone_number, phone_type) VALUES (%s, %s, 'Mobile')", (admin_id, phone))
                    if phone_alt:
                        cursor.execute("INSERT INTO admin_contact (admin_id, phone_number, phone_type) VALUES (%s, %s, 'Alternate')", (admin_id, phone_alt))
                    conn.commit()
                    flash("Admin account created successfully!", "success")
                except Exception as e:
                    flash(f"Error: {str(e)}", "error")
            else:
                flash("All fields are required.", "error")
            return redirect(f"/superadmin/{page}")

        elif page == "configure_courts":
            court_name = request.form.get("court_name")
            court_type = request.form.get("court_type")
            city       = request.form.get("city")
            state      = request.form.get("state")
            if court_name and court_type and city and state:
                try:
                    cursor.execute(
                        "INSERT INTO court (court_name, court_type, city, state) VALUES (%s,%s,%s,%s)",
                        (court_name, court_type, city, state)
                    )
                    conn.commit()
                    flash("Court added successfully!", "success")
                except Exception as e:
                    flash(f"Error: {str(e)}", "error")
            else:
                flash("All court fields are required.", "error")
            return redirect(f"/superadmin/{page}")

    # Shared forms + admin management (superadmin)
    if page in ["add_payment", "update_hearing", "update_verdict"]:
        cursor.execute("""
            SELECT cs.case_id, cs.case_number, cs.case_title, cl.first_name, cl.last_name
            FROM cases cs JOIN client cl ON cs.client_id = cl.client_id
            ORDER BY cs.case_number
        """)
        cases = cursor.fetchall()

        if page == "add_payment":
            cursor.execute("""
                SELECT p.payment_id, p.payment_date, p.amount, p.payment_mode,
                       p.payment_status, p.transaction_reference, cs.case_number,
                       cl.first_name, cl.last_name
                FROM payment p JOIN cases cs ON p.case_id = cs.case_id
                JOIN client cl ON cs.client_id = cl.client_id
                ORDER BY p.payment_date DESC LIMIT 80
            """)
            payments = cursor.fetchall()
            return render_template(
                "buttons/shared/add_payment.html",
                cases=cases, payments=payments,
                back_url="/dashboard/superadmin", form_action="/superadmin/add_payment",
                today=date.today()
            )
        elif page == "update_hearing":
            cursor.execute("""
                SELECT h.*, cs.case_number
                FROM hearing h JOIN cases cs ON h.case_id = cs.case_id
                ORDER BY h.hearing_date DESC, h.hearing_time DESC LIMIT 80
            """)
            hearings = cursor.fetchall()
            return render_template(
                "buttons/shared/update_hearing.html",
                cases=cases, hearings=hearings,
                back_url="/dashboard/superadmin", form_action="/superadmin/update_hearing",
                today=date.today()
            )
        elif page == "update_verdict":
            cursor.execute("""
                SELECT v.*, cs.case_number
                FROM verdict v JOIN cases cs ON v.case_id = cs.case_id
                ORDER BY v.verdict_date DESC LIMIT 80
            """)
            verdicts = cursor.fetchall()
            return render_template(
                "buttons/shared/update_verdict.html",
                cases=cases, verdicts=verdicts,
                back_url="/dashboard/superadmin", form_action="/superadmin/update_verdict",
                today=date.today()
            )

    if page == "manage_admin":
        cursor.execute("SELECT admin_id, first_name, last_name, email, role FROM admin ORDER BY admin_id DESC")
        admins = cursor.fetchall()
        cursor.execute("SELECT advocate_id, first_name, last_name, specialization FROM advocate WHERE is_active=1 ORDER BY first_name")
        advocates = cursor.fetchall()
        return render_template("buttons/supper_admin/manage_admin.html", admins=admins, advocates=advocates)

    # View all clients
    if page == "view_client":
        cursor.execute("SELECT client_id, first_name, last_name, email, phone_number, address FROM client ORDER BY client_id DESC")
        return render_template("buttons/supper_admin/view_client.html", clients=cursor.fetchall())

    # Configure courts
    if page == "configure_courts":
        cursor.execute("SELECT court_id, court_name, court_type, city, state FROM court ORDER BY court_name")
        courts = cursor.fetchall()
        return render_template("buttons/supper_admin/configure_courts.html", courts=courts)

    page_filename = "Financial Reports" if page == "financial_reports" else page

    # Shared folder check
    if os.path.exists(f"templates/buttons/shared/{page_filename}.html"):
        extra = {}
        if page == "directory_view":
            cursor.execute("SELECT court_name, court_type, city, state FROM court ORDER BY court_name")
            extra["courts"] = cursor.fetchall()
        elif page == "financial_reports":
            cursor.execute(
                "SELECT p.payment_id, p.payment_date, p.amount, p.payment_mode, p.payment_status, "
                "c.case_number, cl.first_name, cl.last_name "
                "FROM payment p JOIN cases c ON p.case_id = c.case_id "
                "JOIN client cl ON c.client_id = cl.client_id "
                "ORDER BY p.payment_date DESC"
            )
            extra["payments"] = cursor.fetchall()
            cursor.execute("SELECT COALESCE(SUM(amount),0) AS total FROM payment WHERE payment_status='Completed'")
            extra["total_revenue"] = cursor.fetchone()["total"]
            cursor.execute("SELECT COUNT(*) AS cnt FROM payment WHERE payment_status !='Completed'")
            extra["pending_count"] = cursor.fetchone()["cnt"]
            cursor.execute("SELECT COALESCE(SUM(amount),0) AS total FROM payment WHERE payment_status='Completed' AND MONTH(payment_date)=MONTH(CURDATE()) AND YEAR(payment_date)=YEAR(CURDATE())")
            extra["monthly_collected"] = cursor.fetchone()["total"]
        elif page == "create_case":
            cursor.execute("SELECT client_id, first_name, last_name, email FROM client ORDER BY first_name")
            extra["clients"] = cursor.fetchall()
            cursor.execute(
                "SELECT c.case_id, c.case_number, c.case_title, c.case_type, c.case_status, cl.first_name, cl.last_name "
                "FROM cases c JOIN client cl ON c.client_id = cl.client_id ORDER BY c.filing_date DESC LIMIT 20"
            )
            extra["recent_cases"] = cursor.fetchall()
        elif page == "close_case":
            cursor.execute(
                "SELECT c.case_id, c.case_number, c.case_title, cl.first_name, cl.last_name "
                "FROM cases c JOIN client cl ON c.client_id = cl.client_id "
                "WHERE c.case_status NOT IN ('Closed','Dismissed','Settled') ORDER BY c.filing_date DESC"
            )
            extra["cases"] = cursor.fetchall()
        elif page == "reopen":
            cursor.execute(
                "SELECT c.case_id, c.case_number, c.case_title, c.closing_date, c.case_status, cl.first_name, cl.last_name "
                "FROM cases c JOIN client cl ON c.client_id = cl.client_id "
                "WHERE c.case_status IN ('Closed','Dismissed','Settled') ORDER BY c.closing_date DESC"
            )
            extra["closed_cases"] = cursor.fetchall()
        elif page == "legal_sections":
            cursor.execute(
                "SELECT s.section_id, s.section_no, s.section_title, s.section_description, l.law_name "
                "FROM section s JOIN law l ON s.law_id = l.law_id ORDER BY s.section_no"
            )
            extra["sections"] = cursor.fetchall()

        return render_template(
            f"buttons/shared/{page_filename}.html",
            back_url="/dashboard/superadmin",
            form_action=f"/superadmin/{page}",
            **extra
        )

    if page == "view_advocates":
        cursor.execute("SELECT advocate_id, first_name, last_name, email, specialization, is_active FROM advocate ORDER BY advocate_id DESC")
        return render_template("buttons/supper_admin/view_advocates.html", advocates=cursor.fetchall())

    if page == "view_cases":
        cursor.execute(
            "SELECT c.case_id, c.case_number, c.case_title, c.case_type, c.case_status, "
            "c.description, ct.court_name, cl.first_name, cl.last_name "
            "FROM cases c JOIN client cl ON c.client_id = cl.client_id "
            "LEFT JOIN court ct ON c.court_id = ct.court_id "
            "ORDER BY c.filing_date DESC"
        )
        cases = cursor.fetchall()
        cursor.execute("SELECT court_id, court_name, city, state FROM court ORDER BY court_name")
        courts = cursor.fetchall()
        return render_template("buttons/supper_admin/view_cases.html", cases=cases, courts=courts,
                               back_url="/dashboard/superadmin")

    if page == "view_assignments":
        # SuperAdmin sees all admin->advocate and advocate->client assignments
        cursor.execute("""
            SELECT m.admin_id, m.advocate_id,
                   a.first_name AS admin_first, a.last_name AS admin_last,
                   a.email AS admin_email, a.role AS admin_role,
                   adv.first_name AS adv_first, adv.last_name AS adv_last,
                   adv.email AS adv_email, adv.specialization
            FROM admin_advocate_manage m
            JOIN admin a ON m.admin_id = a.admin_id
            JOIN advocate adv ON m.advocate_id = adv.advocate_id
            ORDER BY a.first_name, adv.first_name
        """)
        admin_advocate_assignments = cursor.fetchall()
        cursor.execute("""
            SELECT ac.advocate_id, ac.client_id,
                   adv.first_name AS adv_first, adv.last_name AS adv_last, adv.specialization,
                   c.first_name AS client_first, c.last_name AS client_last,
                   c.email AS client_email, c.phone_number AS client_phone
            FROM advocate_client ac
            JOIN advocate adv ON ac.advocate_id = adv.advocate_id
            JOIN client c ON ac.client_id = c.client_id
            ORDER BY adv.first_name, c.first_name
        """)
        advocate_client_assignments = cursor.fetchall()
        return render_template("buttons/supper_admin/view_assignments.html",
                               admin_advocate_assignments=admin_advocate_assignments,
                               advocate_client_assignments=advocate_client_assignments)

    if page == "create_case":
        cursor.execute("SELECT client_id, first_name, last_name, email FROM client ORDER BY first_name")
        clients = cursor.fetchall()
        cursor.execute(
            "SELECT c.case_id, c.case_number, c.case_title, c.case_type, c.case_status, cl.first_name, cl.last_name "
            "FROM cases c JOIN client cl ON c.client_id = cl.client_id ORDER BY c.filing_date DESC LIMIT 20"
        )
        recent_cases = cursor.fetchall()
        return render_template(
            "buttons/shared/create_case.html",
            clients=clients, recent_cases=recent_cases,
            back_url="/dashboard/superadmin", form_action="/superadmin/create_case"
        )

    # -------- Manage Advocates (GET) --------
    if page == "manage_advocate":
        cursor.execute(
            "SELECT a.advocate_id, a.first_name, a.last_name, a.email, a.specialization, a.is_active, "
            "ap.photo_url "
            "FROM advocate a LEFT JOIN advocate_profile ap ON a.advocate_id = ap.advocate_id "
            "ORDER BY a.advocate_id DESC"
        )
        return render_template("buttons/supper_admin/manage_advocate.html", advocates=cursor.fetchall())

    # -------- Assign Case (for SuperAdmin via shared template) --------
    if page == "assign_case":
        cursor.execute(
            "SELECT c.case_id, c.case_number, c.case_title, c.case_type, c.case_status, cl.first_name, cl.last_name "
            "FROM cases c JOIN client cl ON c.client_id = cl.client_id ORDER BY c.filing_date DESC"
        )
        cases = cursor.fetchall()
        cursor.execute("SELECT advocate_id, first_name, last_name, specialization FROM advocate WHERE is_active=1 ORDER BY first_name")
        advocates = cursor.fetchall()
        # reuse admin assign_case template with superadmin back_url
        return render_template(
            "buttons/admin/assign_case.html",
            cases=cases, advocates=advocates,
            back_url="/dashboard/superadmin", form_action="/superadmin/assign_case"
        )

    # -------- Update Hearing (superadmin proxy) --------
    if page == "update_hearing":
        if os.path.exists("templates/buttons/admin/update_hearing.html"):
            return render_template(
                "buttons/admin/update_hearing.html",
                back_url="/dashboard/superadmin", form_action="/superadmin/update_hearing"
            )
        flash("Update Hearing page is under construction.", "error")
        return redirect("/dashboard/superadmin")

    # -------- Review Files / Documents (superadmin proxy) --------
    if page == "review_files":
        cursor.execute("SELECT client_id, first_name, last_name, email FROM client ORDER BY first_name")
        clients = cursor.fetchall()
        cursor.execute(
            "SELECT d.document_id, d.document_title, d.document_type, d.file_path, "
            "d.upload_date, d.uploaded_by, d.description, c.case_number, "
            "cl.first_name, cl.last_name "
            "FROM document d "
            "LEFT JOIN cases c ON d.case_id = c.case_id "
            "LEFT JOIN client cl ON d.client_id = cl.client_id OR c.client_id = cl.client_id "
            "ORDER BY d.created_at DESC"
        )
        documents = cursor.fetchall()
        return render_template(
            "buttons/admin/review_files.html",
            clients=clients, documents=documents,
            back_url="/dashboard/superadmin", form_action="/superadmin/review_files"
        )

    # -------- Review Evidence (superadmin proxy) --------
    if page == "review_evidence":
        cursor.execute(
            "SELECT e.evidence_id, e.evidence_title, e.evidence_type, e.description, "
            "e.file_path, e.created_at, c.case_number "
            "FROM evidence e JOIN cases c ON e.case_id = c.case_id ORDER BY e.created_at DESC"
        )
        evidences = cursor.fetchall()
        cursor.execute(
            "SELECT c.case_id, c.case_number, c.case_title FROM cases c "
            "WHERE c.case_status != 'Closed' ORDER BY c.filing_date DESC"
        )
        cases = cursor.fetchall()
        return render_template(
            "buttons/admin/review_evidence.html",
            evidences=evidences, cases=cases,
            back_url="/dashboard/superadmin", form_action="/superadmin/review_evidence"
        )

    if not os.path.exists(f"templates/buttons/supper_admin/{page_filename}.html"):
        flash(f"Page '{page_filename}' is under construction.", "error")
        return redirect("/dashboard/superadmin")

    return render_template(f"buttons/supper_admin/{page_filename}.html")


# ------------- ADVOCATE PAGES -------------

@app.route("/advocate/<page>", methods=["GET", "POST"])
def advocate_pages(page):
    if "advocate_id" not in session:
        flash("Unauthorized access. Please log in.", "error")
        return redirect("/login")

    conn = dbconnect()
    cursor = conn.cursor()

    if request.method == "POST":
        if page == "create_client":
            first_name = request.form.get("first_name")
            last_name  = request.form.get("last_name")
            email      = request.form.get("email")
            phone      = request.form.get("phone_number")
            address    = request.form.get("address")
            password   = request.form.get("password")
            add_case   = request.form.get("add_case")
            
            try:
                hashed_pw = generate_password_hash(password)
                cursor.execute(
                    "INSERT INTO client (first_name, last_name, email, phone_number, address, password_hash) VALUES (%s,%s,%s,%s,%s,%s)",
                    (first_name, last_name, email, phone, address, hashed_pw)
                )
                client_id = cursor.lastrowid
                
                if add_case == "yes":
                    case_number = request.form.get("case_number")
                    case_title  = request.form.get("case_title")
                    case_type   = request.form.get("case_type")
                    court_id    = request.form.get("court_id")
                    description = request.form.get("description")
                    cursor.execute(
                        "INSERT INTO cases (client_id, case_number, case_title, case_type, court_id, case_status, description) VALUES (%s,%s,%s,%s,%s,'Open',%s)",
                        (client_id, case_number, case_title, case_type, court_id, description)
                    )
                    case_id = cursor.lastrowid
                    cursor.execute(
                        "INSERT INTO advocate_case (advocate_id, case_id, role) VALUES (%s,%s,'Lead Counsel')",
                        (session["advocate_id"], case_id)
                    )
                
                # Auto-assign to Advocate
                cursor.execute(
                    "INSERT INTO advocate_client (advocate_id, client_id) VALUES (%s, %s)",
                    (session["advocate_id"], client_id)
                )
                
                conn.commit()
                flash("Client created successfully!", "success")
            except Exception as e:
                flash(f"Error: {str(e)}", "error")
            return redirect(f"/advocate/{page}")

        elif page == "create_case":
            client_id   = request.form.get("client_id")
            case_number = request.form.get("case_number")
            case_title  = request.form.get("case_title")
            case_type   = request.form.get("case_type")
            court_id    = request.form.get("court_id")
            description = request.form.get("description", "")
            filing_date = request.form.get("filing_date") or date.today()
            if client_id and case_number and case_title and case_type and court_id:
                try:
                    cursor.execute(
                        "INSERT INTO cases (client_id, case_number, case_title, case_type, court_id, case_status, description, filing_date) "
                        "VALUES (%s,%s,%s,%s,%s,'Open',%s,%s)",
                        (client_id, case_number, case_title, case_type, court_id, description, filing_date)
                    )
                    case_id = cursor.lastrowid
                    # Auto-link advocate to case
                    cursor.execute(
                        "INSERT INTO advocate_case (advocate_id, case_id, role) VALUES (%s,%s,'Lead Counsel')",
                        (session["advocate_id"], case_id)
                    )
                    conn.commit()
                    flash("Case created and linked to your profile successfully!", "success")
                except Exception as e:
                    flash(f"Error creating case: {str(e)}", "error")
            else:
                flash("Client, case number, title and type are required.", "error")
            return redirect("/advocate/create_case")

        elif page == "upload_evidence":
            submission_type = request.form.get("submission_type") # "Evidence" or "Document"
            client_id       = request.form.get("client_id")
            case_id         = request.form.get("case_id")
            title           = request.form.get("evidence_title")
            doc_type        = request.form.get("evidence_type", "General")
            description     = request.form.get("description", "")
            file_obj        = request.files.get("file_path")
            
            uploaded_by     = session.get("user_name", "Advocate")

            if submission_type == "Evidence":
                filename = save_upload(file_obj, UPLOAD_EVIDENCE)
                if case_id and title and doc_type:
                    try:
                        cursor.execute(
                            "INSERT INTO evidence (case_id, evidence_title, evidence_type, description, file_path) "
                            "VALUES (%s,%s,%s,%s,%s)",
                            (case_id, title, doc_type, description, filename or '')
                        )
                        conn.commit()
                        flash("Evidence submitted successfully!", "success")
                    except Exception as e:
                        flash(f"Submission error: {str(e)}", "error")
                else:
                    flash("Case, title and type are required for Evidence.", "error")
            elif submission_type == "Document":
                filename = save_upload(file_obj, UPLOAD_DOCS)
                if client_id and title:
                    # Provide case_id if present, else None
                    db_case_id = case_id if case_id else None
                    try:
                        cursor.execute(
                            "INSERT INTO document (case_id, client_id, document_title, document_type, file_path, "
                            "upload_date, uploaded_by, description) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            (db_case_id, client_id, title, doc_type, filename or '', date.today(), uploaded_by, description)
                        )
                        conn.commit()
                        flash("Document uploaded successfully!", "success")
                    except Exception as e:
                        flash(f"Upload error: {str(e)}", "error")
                else:
                    flash("Client and title are required for Document.", "error")
            else:
                flash("Invalid submission type.", "error")
                
            return redirect(f"/advocate/{page}")

        elif page in ["add_payment", "update_hearing", "update_verdict"]:
            case_id = request.form.get("case_id")
            # Verify advocate is assigned to this case
            if not case_id:
                flash("Case is required.", "error")
                return redirect(f"/advocate/{page}")
            
            cursor.execute("SELECT 1 FROM advocate_case WHERE advocate_id=%s AND case_id=%s", (session["advocate_id"], case_id))
            if not cursor.fetchone():
                flash("You are not authorized for this case.", "error")
                return redirect(f"/advocate/{page}")

            if page == "add_payment":
                amount   = request.form.get("amount")
                mode     = request.form.get("payment_mode")
                status   = request.form.get("payment_status", "Completed")
                ref      = request.form.get("transaction_reference", "")
                pay_date = request.form.get("payment_date") or date.today()
                if amount and mode:
                    try:
                        cursor.execute(
                            "INSERT INTO payment (case_id, amount, payment_mode, payment_status, payment_date, transaction_reference) "
                            "VALUES (%s,%s,%s,%s,%s,%s)",
                            (case_id, amount, mode, status, pay_date, ref)
                        )
                        conn.commit()
                        flash("Payment recorded successfully!", "success")
                    except Exception as e:
                        flash(f"Payment error: {str(e)}", "error")
                else:
                    flash("Amount and mode are required.", "error")

            elif page == "update_hearing":
                hearing_no = request.form.get("hearing_no")
                hearing_date = request.form.get("hearing_date")
                hearing_time = request.form.get("hearing_time") or None
                hearing_status = request.form.get("hearing_status")
                remarks = request.form.get("remarks", "")
                if hearing_no and hearing_date and hearing_status:
                    try:
                        cursor.execute(
                            "INSERT INTO hearing (case_id, hearing_no, hearing_date, hearing_time, hearing_status, remarks) "
                            "VALUES (%s,%s,%s,%s,%s,%s)",
                            (case_id, hearing_no, hearing_date, hearing_time, hearing_status, remarks)
                        )
                        conn.commit()
                        flash("Hearing details recorded successfully!", "success")
                    except Exception as e:
                        flash(f"Error recording hearing: {str(e)}", "error")
                else:
                    flash("Required fields are missing.", "error")

            elif page == "update_verdict":
                verdict_date = request.form.get("verdict_date")
                result = request.form.get("verdict_result")
                summary = request.form.get("judgement_summary")
                fine = request.form.get("fine_amount") or None
                imprisonment = request.form.get("imprisonment_years") or None
                if verdict_date and result and summary:
                    try:
                        cursor.execute(
                            "INSERT INTO verdict (case_id, verdict_date, verdict_result, judgement_summary, fine_amount, imprisonment_years) "
                            "VALUES (%s,%s,%s,%s,%s,%s)",
                            (case_id, verdict_date, result, summary, fine, imprisonment)
                        )
                        conn.commit()
                        flash("Verdict recorded successfully!", "success")
                    except Exception as e:
                        flash(f"Error recording verdict: {str(e)}", "error")
                else:
                    flash("Required fields are missing.", "error")

            return redirect(f"/advocate/{page}")

        elif page == "review_files":
            cursor.execute("""
                SELECT DISTINCT d.document_id, d.document_title, d.document_type, d.file_path,
                d.upload_date, d.uploaded_by, d.description, c.case_number,
                cl.first_name, cl.last_name
                FROM document d
                LEFT JOIN cases c ON d.case_id = c.case_id
                LEFT JOIN client cl ON d.client_id = cl.client_id OR c.client_id = cl.client_id
                JOIN cases cs ON cs.client_id = cl.client_id
                JOIN advocate_case ac ON ac.case_id = cs.case_id
                WHERE ac.advocate_id = %s
                ORDER BY d.document_id DESC
            """, (session["advocate_id"],))
            return render_template("buttons/advocate/review_files.html", documents=cursor.fetchall())

        elif page == "close_case":
            case_id     = request.form.get("case_id")
            disposition = request.form.get("disposition", "Closed")
            close_date  = request.form.get("close_date") or date.today()
            remarks     = request.form.get("final_remarks", "")
            
            if case_id:
                # Verify advocate is authorized for this case (either direct case assignment or client link)
                cursor.execute("""
                    SELECT 1 FROM advocate_case WHERE advocate_id=%s AND case_id=%s
                    UNION
                    SELECT 1 FROM cases cs
                    JOIN advocate_client acl ON acl.client_id = cs.client_id
                    WHERE acl.advocate_id=%s AND cs.case_id=%s
                    LIMIT 1
                """, (session["advocate_id"], case_id, session["advocate_id"], case_id))
                if not cursor.fetchone():
                    flash("You are not authorized to close this case.", "error")
                    return redirect(f"/advocate/{page}")
                
                try:
                    cursor.execute(
                        "UPDATE cases SET case_status=%s, closing_date=%s, description=CONCAT(IFNULL(description,''), '\n[Closed] ', %s) WHERE case_id=%s",
                        (disposition, close_date, remarks, case_id)
                    )
                    conn.commit()
                    flash("Case closed successfully!", "success")
                except Exception as e:
                    flash(f"Error closing case: {str(e)}", "error")
            else:
                flash("Please select a case to close.", "error")
            return redirect(f"/advocate/{page}")

    # Shared folder check first (but skip pages that need advocate-specific data loading)
    _advocate_specific_pages = {"close_case", "add_payment", "update_hearing", "update_verdict"}
    if os.path.exists(f"templates/buttons/shared/{page}.html") and page not in _advocate_specific_pages:
        extra = {}
        if page == "directory_view":
            cursor.execute("SELECT court_name, court_type, city, state FROM court ORDER BY court_name")
            extra["courts"] = cursor.fetchall()
        elif page == "view_client":
            # Clients assigned directly or through cases
            cursor.execute("""
                SELECT DISTINCT c.client_id, c.first_name, c.last_name, c.email, c.phone_number,
                       cs.filing_date AS assigned_at
                FROM client c
                LEFT JOIN advocate_client ac2 ON ac2.client_id = c.client_id AND ac2.advocate_id = %s
                LEFT JOIN cases cs ON cs.client_id = c.client_id
                LEFT JOIN advocate_case ac ON ac.case_id = cs.case_id AND ac.advocate_id = %s
                WHERE ac2.advocate_id = %s OR ac.advocate_id = %s
                ORDER BY c.first_name
            """, (session["advocate_id"], session["advocate_id"], session["advocate_id"], session["advocate_id"]))
            extra["clients"] = cursor.fetchall()
        elif page == "create_client":
            cursor.execute("SELECT court_id, court_name, city FROM court ORDER BY court_name")
            extra["courts"] = cursor.fetchall()
        elif page == "create_case":
            cursor.execute("SELECT client_id, first_name, last_name, email FROM client ORDER BY first_name")
            extra["clients"] = cursor.fetchall()
            cursor.execute("SELECT court_id, court_name, city FROM court ORDER BY court_name")
            extra["courts"] = cursor.fetchall()
            cursor.execute(
                "SELECT c.case_id, c.case_number, c.case_title, c.case_type, c.case_status, cl.first_name, cl.last_name "
                "FROM cases c JOIN client cl ON c.client_id = cl.client_id "
                "JOIN advocate_case ac ON ac.case_id = c.case_id "
                "WHERE ac.advocate_id = %s ORDER BY c.filing_date DESC LIMIT 20",
                (session["advocate_id"],)
            )
            extra["recent_cases"] = cursor.fetchall()
        elif page == "legal_sections":
            cursor.execute(
                "SELECT s.section_id, s.section_no, s.section_title, s.section_description, l.law_name "
                "FROM section s JOIN law l ON s.law_id = l.law_id ORDER BY s.section_no"
            )
            extra["sections"] = cursor.fetchall()

        return render_template(
            f"buttons/shared/{page}.html",
            back_url="/dashboard/advocate",
            form_action=f"/advocate/{page}",
            **extra
        )

    if not os.path.exists(f"templates/buttons/advocate/{page}.html") and page not in _advocate_specific_pages:
        flash(f"Page '{page}' is under construction.", "error")
        return redirect("/dashboard/advocate")

    # View assigned clients for this advocate (via direct link OR case assignment)
    if page == "view_client":
        cursor.execute("""
            SELECT DISTINCT c.client_id, c.first_name, c.last_name, c.email, c.phone_number, c.address
            FROM client c
            WHERE c.client_id IN (
                SELECT client_id FROM advocate_client WHERE advocate_id = %s
                UNION
                SELECT cs.client_id FROM cases cs
                JOIN advocate_case ac ON ac.case_id = cs.case_id
                WHERE ac.advocate_id = %s
            )
            ORDER BY c.first_name
        """, (session["advocate_id"], session["advocate_id"]))
        return render_template(f"buttons/advocate/{page}.html", clients=cursor.fetchall())

    # Active cases for this advocate (via direct assignment OR client link)
    if page == "cases_view":
        cursor.execute("""
            SELECT DISTINCT cs.case_id, cs.case_number, cs.case_title, cl.first_name, cl.last_name,
                   cs.case_type, cs.case_status, cs.filing_date, cs.description
            FROM cases cs
            JOIN client cl ON cs.client_id = cl.client_id
            WHERE cs.case_status NOT IN ('Closed', 'Dismissed', 'Settled')
              AND cs.case_id IN (
                SELECT case_id FROM advocate_case WHERE advocate_id = %s
                UNION
                SELECT cs2.case_id FROM cases cs2
                JOIN advocate_client acl ON acl.client_id = cs2.client_id
                WHERE acl.advocate_id = %s
            )
            ORDER BY cs.filing_date DESC
        """, (session["advocate_id"], session["advocate_id"]))
        return render_template(f"buttons/advocate/{page}.html", cases=cursor.fetchall())

    if page == "closed_archive":
        cursor.execute("""
            SELECT DISTINCT cs.case_id, cs.case_number, cl.first_name, cl.last_name,
                   cs.closing_date, cs.case_status, cs.description
            FROM cases cs
            JOIN client cl ON cs.client_id = cl.client_id
            WHERE cs.case_status IN ('Closed', 'Dismissed', 'Settled')
              AND cs.case_id IN (
                SELECT case_id FROM advocate_case WHERE advocate_id = %s
                UNION
                SELECT cs2.case_id FROM cases cs2
                JOIN advocate_client acl ON acl.client_id = cs2.client_id
                WHERE acl.advocate_id = %s
            )
            ORDER BY cs.closing_date DESC
        """, (session["advocate_id"], session["advocate_id"]))
        return render_template(f"buttons/advocate/{page}.html", archives=cursor.fetchall())

    if page == "close_case":
        cursor.execute("""
            SELECT DISTINCT cs.case_id, cs.case_number, cs.case_title, cl.first_name, cl.last_name, cs.filing_date
            FROM cases cs
            JOIN client cl ON cs.client_id = cl.client_id
            WHERE cs.case_status NOT IN ('Closed', 'Dismissed', 'Settled')
              AND cs.case_id IN (
                SELECT case_id FROM advocate_case WHERE advocate_id = %s
                UNION
                SELECT cs2.case_id FROM cases cs2
                JOIN advocate_client acl ON acl.client_id = cs2.client_id
                WHERE acl.advocate_id = %s
            )
            ORDER BY cs.filing_date DESC
        """, (session["advocate_id"], session["advocate_id"]))
        return render_template(
            "buttons/shared/close_case.html",
            cases=cursor.fetchall(),
            back_url="/dashboard/advocate",
            form_action="/advocate/close_case"
        )

    if page == "upload_evidence":
        cursor.execute("""
            SELECT DISTINCT c.client_id, c.first_name, c.last_name
            FROM client c
            WHERE c.client_id IN (
                SELECT client_id FROM advocate_client WHERE advocate_id = %s
                UNION
                SELECT cs.client_id FROM cases cs
                JOIN advocate_case ac ON ac.case_id = cs.case_id
                WHERE ac.advocate_id = %s
            )
            ORDER BY c.first_name
        """, (session["advocate_id"], session["advocate_id"]))
        clients = cursor.fetchall()
        return render_template(f"buttons/advocate/{page}.html", clients=clients)

    if page == "create_case":
        # Show clients assigned to this advocate via direct link OR through case assignment
        cursor.execute("""
            SELECT DISTINCT c.client_id, c.first_name, c.last_name, c.email
            FROM client c
            WHERE c.client_id IN (
                SELECT client_id FROM advocate_client WHERE advocate_id = %s
                UNION
                SELECT cs.client_id FROM cases cs
                JOIN advocate_case ac ON ac.case_id = cs.case_id
                WHERE ac.advocate_id = %s
            )
            ORDER BY c.first_name
        """, (session["advocate_id"], session["advocate_id"]))
        clients = cursor.fetchall()
        cursor.execute("""
            SELECT cs.case_id, cs.case_number, cs.case_title, cs.case_type, cs.case_status,
                   cl.first_name, cl.last_name
            FROM cases cs
            JOIN client cl ON cs.client_id = cl.client_id
            JOIN advocate_case ac ON ac.case_id = cs.case_id
            WHERE ac.advocate_id = %s
            ORDER BY cs.filing_date DESC LIMIT 20
        """, (session["advocate_id"],))
        recent_cases = cursor.fetchall()
        return render_template(
            "buttons/shared/create_case.html",
            clients=clients, recent_cases=recent_cases,
            back_url="/dashboard/advocate", form_action="/advocate/create_case"
        )

    if page in ["add_payment", "update_hearing", "update_verdict"]:
        advocate_id = session["advocate_id"]
        cursor.execute("""
            SELECT DISTINCT cs.case_id, cs.case_number, cs.case_title, cl.first_name, cl.last_name, cs.filing_date
            FROM cases cs
            JOIN client cl ON cs.client_id = cl.client_id
            WHERE cs.case_id IN (
                SELECT case_id FROM advocate_case WHERE advocate_id = %s
                UNION
                SELECT cs2.case_id FROM cases cs2
                JOIN advocate_client acl ON acl.client_id = cs2.client_id
                WHERE acl.advocate_id = %s
            )
            ORDER BY cs.case_number
        """, (advocate_id, advocate_id))
        cases = cursor.fetchall()
        
        if page == "add_payment":
            cursor.execute("""
                SELECT p.payment_id, p.payment_date, p.amount, p.payment_mode,
                       p.payment_status, p.transaction_reference, cs.case_number,
                       cl.first_name, cl.last_name
                FROM payment p
                JOIN cases cs ON p.case_id = cs.case_id
                JOIN client cl ON cs.client_id = cl.client_id
                JOIN advocate_case ac ON ac.case_id = cs.case_id
                WHERE ac.advocate_id = %s
                ORDER BY p.payment_date DESC LIMIT 50
            """, (advocate_id,))
            payments = cursor.fetchall()
            return render_template(
                "buttons/shared/add_payment.html",
                cases=cases, payments=payments,
                back_url="/dashboard/advocate", form_action="/advocate/add_payment",
                today=date.today()
            )
        elif page == "update_hearing":
            cursor.execute("""
                SELECT h.*, cs.case_number
                FROM hearing h
                JOIN cases cs ON h.case_id = cs.case_id
                JOIN advocate_case ac ON ac.case_id = cs.case_id
                WHERE ac.advocate_id = %s
                ORDER BY h.hearing_date DESC, h.hearing_time DESC LIMIT 50
            """, (advocate_id,))
            hearings = cursor.fetchall()
            return render_template(
                "buttons/shared/update_hearing.html",
                cases=cases, hearings=hearings,
                back_url="/dashboard/advocate", form_action="/advocate/update_hearing",
                today=date.today()
            )
        elif page == "update_verdict":
            cursor.execute("""
                SELECT v.*, cs.case_number
                FROM verdict v
                JOIN cases cs ON v.case_id = cs.case_id
                JOIN advocate_case ac ON ac.case_id = cs.case_id
                WHERE ac.advocate_id = %s
                ORDER BY v.verdict_date DESC LIMIT 50
            """, (advocate_id,))
            verdicts = cursor.fetchall()
            return render_template(
                "buttons/shared/update_verdict.html",
                cases=cases, verdicts=verdicts,
                back_url="/dashboard/advocate", form_action="/advocate/update_verdict",
                today=date.today()
            )

    return render_template(f"buttons/advocate/{page}.html")


# ------------- CLIENT PAGES -------------

@app.route("/client/<page>")
def client_pages(page):
    if "client_id" not in session:
        flash("Unauthorized access. Please log in.", "error")
        return redirect("/login")

    conn = dbconnect()
    cursor = conn.cursor()
    client_id = session["client_id"]

    if not os.path.exists(f"templates/buttons/client/{page}.html"):
        flash(f"Page '{page}' is under construction.", "error")
        return redirect("/dashboard/client")

    if page == "case_status":
        cursor.execute("""
            SELECT cs.case_id, cs.case_number, cs.case_title, cs.case_type,
                   cs.case_status, cs.filing_date, cs.description
            FROM cases cs
            WHERE cs.client_id = %s
            ORDER BY cs.filing_date DESC
        """, (client_id,))
        cases = cursor.fetchall()
        return render_template("buttons/client/case_status.html", cases=cases)

    if page == "hearing":
        # Next upcoming hearing
        cursor.execute("""
            SELECT h.hearing_date, h.hearing_time, h.hearing_status, h.remarks,
                   cs.case_number, cs.case_title
            FROM hearing h
            JOIN cases cs ON h.case_id = cs.case_id
            WHERE cs.client_id = %s AND h.hearing_date >= CURDATE()
            ORDER BY h.hearing_date ASC
            LIMIT 1
        """, (client_id,))
        next_hearing = cursor.fetchone()

        # All hearings history
        cursor.execute("""
            SELECT h.hearing_no, h.hearing_date, h.hearing_time, h.hearing_status, h.remarks,
                   cs.case_number
            FROM hearing h
            JOIN cases cs ON h.case_id = cs.case_id
            WHERE cs.client_id = %s
            ORDER BY h.hearing_date DESC
        """, (client_id,))
        hearings = cursor.fetchall()
        return render_template("buttons/client/hearing.html", next_hearing=next_hearing, hearings=hearings)

    if page == "payment_history":
        cursor.execute("""
            SELECT p.payment_id, p.payment_date, p.amount, p.payment_mode,
                   p.payment_status, p.transaction_reference,
                   cs.case_number, cs.case_title
            FROM payment p
            JOIN cases cs ON p.case_id = cs.case_id
            WHERE cs.client_id = %s
            ORDER BY p.payment_date DESC
        """, (client_id,))
        payments = cursor.fetchall()
        return render_template("buttons/client/payment_history.html", payments=payments)

    if page == "court_details":
        cursor.execute("""
            SELECT cs.case_id, cs.case_number, cs.case_title, cs.case_status,
                   ct.court_id, ct.court_name, ct.court_type, ct.city, ct.state, ct.country,
                   h.hearing_date, h.hearing_time, h.hearing_status, h.remarks
            FROM cases cs
            LEFT JOIN court ct ON cs.court_id = ct.court_id
            LEFT JOIN hearing h ON h.case_id = cs.case_id
                AND h.hearing_date = (
                    SELECT MIN(h2.hearing_date) FROM hearing h2
                    WHERE h2.case_id = cs.case_id AND h2.hearing_date >= CURDATE()
                )
            WHERE cs.client_id = %s
            ORDER BY cs.filing_date DESC
        """, (client_id,))
        cases_with_courts = cursor.fetchall()
        return render_template("buttons/client/court_details.html", cases=cases_with_courts)

    if page == "verdict":
        cursor.execute("""
            SELECT v.verdict_id, v.verdict_date, v.verdict_result,
                   v.judgement_summary, v.fine_amount, v.imprisonment_years,
                   cs.case_number, cs.case_title
            FROM verdict v
            JOIN cases cs ON v.case_id = cs.case_id
            WHERE cs.client_id = %s
            ORDER BY v.verdict_date DESC
        """, (client_id,))
        verdicts = cursor.fetchall()
        return render_template("buttons/client/verdict.html", verdicts=verdicts)

    return render_template(f"buttons/client/{page}.html")


# ---------------- DELETE ROUTE ----------------
@app.route("/api/delete/<record_type>/<int:record_id>", methods=["POST"])
def delete_record(record_type, record_id):
    # Role detection
    is_superadmin = "superadmin_id" in session
    is_admin      = "admin_id" in session
    is_advocate   = "advocate_id" in session

    if not any([is_superadmin, is_admin, is_advocate]):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    conn = dbconnect()
    cursor = conn.cursor()

    try:
        # SUPERADMIN PERMISSIONS (Can delete: cases, court, admin, advocate, section, client)
        if is_superadmin:
            if record_type == "case":
                cursor.execute("DELETE FROM cases WHERE case_id=%s", (record_id,))
            elif record_type == "court":
                cursor.execute("DELETE FROM court WHERE court_id=%s", (record_id,))
            elif record_type == "admin":
                cursor.execute("DELETE FROM admin WHERE admin_id=%s", (record_id,))
            elif record_type == "advocate":
                cursor.execute("DELETE FROM advocate WHERE advocate_id=%s", (record_id,))
            elif record_type == "section":
                cursor.execute("DELETE FROM section WHERE section_id=%s", (record_id,))
            elif record_type == "client":
                cursor.execute("DELETE FROM client WHERE client_id=%s", (record_id,))
            else:
                return jsonify({"success": False, "error": f"Invalid type for SuperAdmin: {record_type}"}), 400
        
        # ADMIN PERMISSIONS (Can delete: cases, admin, advocate, client. Cannot delete court, section)
        elif is_admin:
            if record_type in ["court", "section"]:
                return jsonify({"success": False, "error": "Admin cannot delete courts or sections."}), 403
            if record_type == "case":
                cursor.execute("DELETE FROM cases WHERE case_id=%s", (record_id,))
            elif record_type == "admin":
                cursor.execute("DELETE FROM admin WHERE admin_id=%s", (record_id,))
            elif record_type == "advocate":
                cursor.execute("DELETE FROM advocate WHERE advocate_id=%s", (record_id,))
            elif record_type == "client":
                cursor.execute("DELETE FROM client WHERE client_id=%s", (record_id,))
            else:
                return jsonify({"success": False, "error": f"Invalid type for Admin: {record_type}"}), 400

        # ADVOCATE PERMISSIONS (Can delete: client, case ONLY IF ASSIGNED)
        elif is_advocate:
            advocate_id = session.get("advocate_id")
            if record_type == "client":
                # Check if advocate_client table has the link
                cursor.execute("SELECT 1 FROM advocate_client WHERE advocate_id=%s AND client_id=%s", (advocate_id, record_id))
                has_link = cursor.fetchone()
                
                # Or check if advocate is assigned to a case that belongs to this client
                if not has_link:
                    cursor.execute("""
                        SELECT 1 FROM cases cs
                        JOIN advocate_case ac ON cs.case_id = ac.case_id
                        WHERE ac.advocate_id=%s AND cs.client_id=%s
                    """, (advocate_id, record_id))
                    has_link = cursor.fetchone()
                
                if has_link:
                    cursor.execute("DELETE FROM client WHERE client_id=%s", (record_id,))
                else:
                    return jsonify({"success": False, "error": "Advocate cannot delete this client. Not assigned."}), 403

            elif record_type == "case":
                # Check if advocate is assigned to this case
                cursor.execute("SELECT 1 FROM advocate_case WHERE advocate_id=%s AND case_id=%s", (advocate_id, record_id))
                if cursor.fetchone():
                    cursor.execute("DELETE FROM cases WHERE case_id=%s", (record_id,))
                else:
                    return jsonify({"success": False, "error": "Advocate cannot delete this case. Not assigned."}), 403
            else:
                return jsonify({"success": False, "error": "Advocate can only delete clients and cases."}), 403

        if cursor.rowcount > 0:
            conn.commit()
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Record not found or already deleted."}), 404

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------- UPDATE ROUTE ----------------
@app.route("/api/update/<record_type>/<int:record_id>", methods=["POST"])
def update_record(record_type, record_id):
    # Role detection
    is_superadmin = "superadmin_id" in session
    is_admin      = "admin_id" in session
    is_advocate   = "advocate_id" in session

    if not any([is_superadmin, is_admin, is_advocate]):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    conn = dbconnect()
    cursor = conn.cursor()
    data = request.form

    try:
        if record_type == "client":
            if is_advocate:  # Simple check, full check would be like delete
                advocate_id = session.get("advocate_id")
                cursor.execute("""
                    SELECT 1 FROM cases cs JOIN advocate_case ac ON cs.case_id = ac.case_id
                    WHERE ac.advocate_id=%s AND cs.client_id=%s
                """, (advocate_id, record_id))
                if not cursor.fetchone():
                    return jsonify({"success": False, "error": "Not assigned to this client."}), 403

            cursor.execute("""
                UPDATE client SET 
                first_name=%s, last_name=%s, email=%s, phone_number=%s, address=%s 
                WHERE client_id=%s
            """, (
                data.get("first_name"), data.get("last_name"), data.get("email"), 
                data.get("phone_number"), data.get("address"), record_id
            ))
            
        elif record_type == "payment" and (is_superadmin or is_admin or is_advocate):
            cursor.execute("""
                UPDATE payment SET
                amount=%s, payment_mode=%s, payment_status=%s,
                payment_date=%s, transaction_reference=%s
                WHERE payment_id=%s
            """, (
                data.get("amount"), data.get("payment_mode"), data.get("payment_status"),
                data.get("payment_date"), data.get("transaction_reference"), record_id
            ))

        elif record_type == "hearing" and (is_superadmin or is_admin or is_advocate):
            cursor.execute("""
                UPDATE hearing SET
                hearing_no=%s, hearing_date=%s, hearing_time=%s,
                hearing_status=%s, remarks=%s
                WHERE hearing_id=%s
            """, (
                data.get("hearing_no"), data.get("hearing_date"), data.get("hearing_time") or None,
                data.get("hearing_status"), data.get("remarks"), record_id
            ))

        elif record_type == "verdict" and (is_superadmin or is_admin or is_advocate):
            cursor.execute("""
                UPDATE verdict SET
                verdict_date=%s, verdict_result=%s,
                judgement_summary=%s, fine_amount=%s, imprisonment_years=%s
                WHERE verdict_id=%s
            """, (
                data.get("verdict_date"), data.get("verdict_result"),
                data.get("judgement_summary"),
                data.get("fine_amount") or None, data.get("imprisonment_years") or None,
                record_id
            ))

        elif record_type == "case" and (is_superadmin or is_admin or is_advocate):
            # Advocates can only update their own cases
            if is_advocate:
                advocate_id = session.get("advocate_id")
                cursor.execute("SELECT 1 FROM advocate_case WHERE advocate_id=%s AND case_id=%s", (advocate_id, record_id))
                if not cursor.fetchone():
                    return jsonify({"success": False, "error": "Not authorized for this case."}), 403
            cursor.execute("""
                UPDATE cases SET
                case_title=%s, case_type=%s, case_status=%s, description=%s
                WHERE case_id=%s
            """, (
                data.get("case_title"), data.get("case_type"),
                data.get("case_status"), data.get("description"), record_id
            ))

        elif record_type == "advocate" and (is_superadmin or is_admin):
            # Update main table
            cursor.execute("""
                UPDATE advocate SET 
                first_name=%s, last_name=%s, email=%s, specialization=%s 
                WHERE advocate_id=%s
            """, (
                data.get("first_name"), data.get("last_name"), data.get("email"), 
                data.get("specialization"), record_id
            ))
            
            # Normalize phone numbers
            phone = normalize_phone_from_form(data, "phone_number")
            phone_alt = normalize_phone_from_form(data, "phone_number_alt")

            # Update contact
            cursor.execute("DELETE FROM advocate_contact WHERE advocate_id=%s", (record_id,))
            if phone:
                cursor.execute("INSERT INTO advocate_contact (advocate_id, phone_number, phone_type) VALUES (%s, %s, 'Mobile')", (record_id, phone))
            if phone_alt:
                cursor.execute("INSERT INTO advocate_contact (advocate_id, phone_number, phone_type) VALUES (%s, %s, 'Alternate')", (record_id, phone_alt))
            
        elif record_type == "admin" and is_superadmin:
            # Update main table
            cursor.execute("""
                UPDATE admin SET 
                first_name=%s, last_name=%s, email=%s 
                WHERE admin_id=%s
            """, (
                data.get("first_name"), data.get("last_name"), data.get("email"), record_id
            ))
            
            # Normalize phone number
            phone = normalize_phone_from_form(data, "phone_number")
            
            # Update contact
            if phone:
                cursor.execute("DELETE FROM admin_contact WHERE admin_id=%s", (record_id,))
                cursor.execute("INSERT INTO admin_contact (admin_id, phone_number, phone_type) VALUES (%s, %s, 'Mobile')", (record_id, phone))
        else:
            return jsonify({"success": False, "error": f"Invalid type or unauthorized for {record_type}"}), 400

        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------- ASSIGN COURT TO CASE ----------------
@app.route("/api/case/<int:case_id>/assign_court", methods=["POST"])
def assign_court_to_case(case_id):
    if not any(k in session for k in ["superadmin_id", "admin_id", "advocate_id"]):
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    court_id = request.form.get("court_id")
    if not court_id:
        return jsonify({"success": False, "error": "court_id is required"}), 400
    conn = dbconnect()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE cases SET court_id=%s WHERE case_id=%s", (court_id, case_id))
        if cursor.rowcount == 0:
            return jsonify({"success": False, "error": "Case not found"}), 404
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------- UNASSIGN ROUTES ----------------
@app.route("/api/unassign/admin_advocate", methods=["POST"])
def unassign_admin_advocate():
    if "superadmin_id" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    admin_id = request.form.get("admin_id")
    advocate_id = request.form.get("advocate_id")
    if not admin_id or not advocate_id:
        return jsonify({"success": False, "error": "admin_id and advocate_id are required"}), 400
    conn = dbconnect()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM admin_advocate_manage WHERE admin_id=%s AND advocate_id=%s", (admin_id, advocate_id))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/unassign/advocate_client", methods=["POST"])
def unassign_advocate_client():
    if not any(k in session for k in ["superadmin_id", "admin_id"]):
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    advocate_id = request.form.get("advocate_id")
    client_id = request.form.get("client_id")
    if not advocate_id or not client_id:
        return jsonify({"success": False, "error": "advocate_id and client_id are required"}), 400
    conn = dbconnect()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM advocate_client WHERE advocate_id=%s AND client_id=%s", (advocate_id, client_id))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# Logout
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


if __name__ == "__main__":
    app.run(debug=True)