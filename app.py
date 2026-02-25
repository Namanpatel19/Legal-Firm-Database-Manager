from flask import Flask, render_template, request, redirect, session, flash
from flask_mysqldb import MySQL
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from config import Config
from datetime import date
import os

app = Flask(__name__)
app.config.from_object(Config)
mysql = MySQL(app)

# Register
@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = generate_password_hash(request.form['password'])
        role = request.form['role']

        cur = mysql.connection.cursor()
        cur.execute("INSERT INTO User(name,email,password,role) VALUES(%s,%s,%s,%s)",
                    (name,email,password,role))
        mysql.connection.commit()

        user_id = cur.lastrowid
        if role == 'Advocate':
            cur.execute("INSERT INTO Advocate(advocate_id) VALUES(%s)",(user_id,))
            mysql.connection.commit()

        cur.close()
        return redirect('/login')
    return render_template('register.html')

# Login
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        cur = mysql.connection.cursor()
        cur.execute("SELECT * FROM User WHERE email=%s",(email,))
        user = cur.fetchone()
        cur.close()

        if user and check_password_hash(user[3],password):
            session['user_id'] = user[0]
            session['role'] = user[4]

            if user[4]=='Admin':
                return redirect('/admin_dashboard')
            else:
                return redirect('/advocate_dashboard')
        else:
            flash("Invalid Credentials")
    return render_template('login.html')

# Dashboards
@app.route('/admin_dashboard')
def admin_dashboard():
    if session.get('role')!='Admin':
        return redirect('/login')
    return render_template('admin_dashboard.html')

@app.route('/advocate_dashboard')
def advocate_dashboard():
    if session.get('role')!='Advocate':
        return redirect('/login')
    return render_template('advocate_dashboard.html')

# Clients
@app.route('/clients')
def clients():
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM Client")
    data = cur.fetchall()
    cur.close()
    return render_template('clients.html', clients=data)

@app.route('/add_client', methods=['POST'])
def add_client():
    name = request.form['name']
    contact = request.form['contact']
    address = request.form['address']
    email = request.form['email']

    cur = mysql.connection.cursor()
    cur.execute("INSERT INTO Client(name,contact,address,email) VALUES(%s,%s,%s,%s)",
                (name,contact,address,email))
    mysql.connection.commit()
    cur.close()
    return redirect('/clients')

# Hearings
@app.route('/hearings')
def hearings():
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM Hearing")
    data = cur.fetchall()
    cur.close()
    return render_template('hearings.html', hearings=data)

@app.route('/add_hearing', methods=['POST'])
def add_hearing():
    case_id = request.form['case_id']
    hearing_date = request.form['hearing_date']
    remarks = request.form['remarks']
    next_date = request.form['next_hearing_date']

    cur = mysql.connection.cursor()
    cur.execute("INSERT INTO Hearing(case_id,hearing_date,remarks,next_hearing_date) VALUES(%s,%s,%s,%s)",
                (case_id,hearing_date,remarks,next_date))
    mysql.connection.commit()
    cur.close()
    return redirect('/hearings')

# Evidence
@app.route('/evidence')
def evidence():
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM Evidence")
    data = cur.fetchall()
    cur.close()
    return render_template('evidence.html', evidence=data)

@app.route('/upload_evidence', methods=['POST'])
def upload_evidence():
    case_id = request.form['case_id']
    file = request.files['file']

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    cur = mysql.connection.cursor()
    cur.execute("INSERT INTO Evidence(case_id,document_name,upload_date,file_path) VALUES(%s,%s,%s,%s)",
                (case_id,filename,date.today(),filepath))
    mysql.connection.commit()
    cur.close()
    return redirect('/evidence')

# Logout
@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

if __name__ == '__main__':
    if not os.path.exists('uploads'):
        os.makedirs('uploads')
    app.run(debug=True)