from flask import Flask, request, redirect, session, render_template_string
import requests
import os

app = Flask(__name__)
app.secret_key = "change_me"
LOG_DB_URL = os.environ.get("LOG_DB_URL", "http://log-db:5000")
DEFAULT_USER = "analyst"
DEFAULT_PASSWORD = "Password123!"

LOGIN_TEMPLATE = """
<!DOCTYPE html><html><body>
<h2>Log Analysis Portal</h2>
{% if error %}<p style="color:red">{{ error }}</p>{% endif %}
<form method="post">
<input name="username" placeholder="Username"><br><br>
<input type="password" name="password" placeholder="Password"><br><br>
<input type="submit" value="Login">
</form></body></html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html><html><body>
<h1>Security Analysis Dashboard</h1>
<p>Logged in as {{ user }} | <a href="/logout">Logout</a></p>
<table border="1">
<tr><th>ID</th><th>Source</th><th>Severity</th><th>Log Line</th><th>Analysis</th></tr>
{% for row in records %}
<tr><td>{{ row.id }}</td><td>{{ row.source }}</td><td>{{ row.severity }}</td><td>{{ row.log_line }}</td><td>{{ row.analysis }}</td></tr>
{% endfor %}
</table></body></html>
"""

@app.route('/', methods=['GET','POST'])
def login():
    if request.method=='POST':
        if request.form.get('username')==DEFAULT_USER and request.form.get('password')==DEFAULT_PASSWORD:
            session['user']=DEFAULT_USER
            return redirect('/dashboard')
        return render_template_string(LOGIN_TEMPLATE,error='Invalid username or password')
    return render_template_string(LOGIN_TEMPLATE,error=None)

@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect('/')
    try:
        r=requests.get(f"{LOG_DB_URL}/records?limit=100",timeout=10)
        r.raise_for_status()
        records=r.json()
    except Exception as exc:
        records=[{'id':'-','source':'system','severity':'error','log_line':'Unable to retrieve records','analysis':str(exc)}]
    return render_template_string(DASHBOARD_TEMPLATE,user=session['user'],records=records)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081)
