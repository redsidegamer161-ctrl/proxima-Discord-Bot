from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Proxima Bot is Online and Alive!"

def run():
    # Binds to port 8080, which Render requires for web services
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()
