import os
import sqlite3
import csv
import io
import json
import logging
import random
import string
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, flash, session, send_file, jsonify, Response
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from jinja2 import ChoiceLoader, DictLoader

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(threadName)s : %(message)s',
    filename='platform.log'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

DB_NAME = "gamified_learning.db"

def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

# ==========================================
# QUIZ AUTO-GENERATION
# ==========================================
DIFF_LABELS = {'easy': 'Facile', 'medium': 'Moyen', 'hard': 'Difficile'}
DIFF_ORDER = {'easy': 1, 'medium': 2, 'hard': 3}

# ==========================================
# NARRATION ET SCENARIOS IMMERSIFS
# ==========================================
MISSIONS_STORY = {
    1: ("🛡️ Mission Alpha : L'algorithme secret", "Le cryptage a résisté. L'ordinateur central demande la séquence exacte.", "🎯 Séquence validée ! L'accès au réseau est déverrouillé."),
    2: ("⚙️ Mission Beta : Réparer le core", "Les instructions sont corrompues. Le système d'exploitation plante.", "⚡ Core réparé ! Les systèmes secondaires sont en ligne."),
    3: ("🗺️ Mission Gamma : Le labyrinthe séquentiel", "Vous vous êtes heurté à un mur logique. Le chemin est bloqué.", "🚪 Porte ouverte ! Vous avez trouvé la sortie du labyrinthe."),
    4: ("🔀 Mission Delta : La bifurcation critique", "Mauvaise branche choisie. Le signal est perdu.", "📡 Signal récupéré ! La bonne voie a été sélectionnée."),
    5: ("🐍 Mission Epsilon : Transcription Python", "Erreur de syntaxe dans le traducteur. Le code source est illisible.", "💻 Code compilé avec succès ! Le programme s'exécute."),
    6: ("🖥️ Mission Zeta : L'implémentation finale", "Le programme ne répond pas. La logique est défaillante.", "🚀 Programme déployé ! L'interface est opérationnelle.")
}

def get_narrative(subcategory_id):
    # Pour la démo, on force l'histoire de la Mission Alpha (ID 1) si l'ID n'est pas trouvé
    story = MISSIONS_STORY.get(subcategory_id)
    if story:
        return story
    
    # Si l'ID n'est pas dans la liste, on renvoie quand même la Mission Alpha
    return MISSIONS_STORY.get(1) 
def get_or_create_quiz(subcategory_id, difficulty):
    """Crée ou rafraîchit un quiz auto-généré pour une sous-catégorie + difficulté."""
    with get_db() as conn:
        questions = conn.execute(
            "SELECT id FROM questions WHERE subcategory_id=? AND difficulty=?",
            (subcategory_id, difficulty)
        ).fetchall()

        quiz = conn.execute(
            "SELECT id FROM quizzes WHERE subcategory_id=? AND difficulty=? AND is_daily_challenge=0",
            (subcategory_id, difficulty)
        ).fetchone()

        if not questions and quiz:
            conn.execute("DELETE FROM quiz_questions WHERE quiz_id=?", (quiz['id'],))
            conn.execute("DELETE FROM quizzes WHERE id=?", (quiz['id'],))
            return None

        if quiz:
            quiz_id = quiz['id']
            conn.execute("DELETE FROM quiz_questions WHERE quiz_id=?", (quiz_id,))
            for q in questions:
                conn.execute("INSERT INTO quiz_questions VALUES (?,?)", (quiz_id, q['id']))
            return quiz_id

        if not questions:
            return None

        subcat = conn.execute('''
            SELECT sc.name, c.id as cat_id FROM subcategories sc
            JOIN categories c ON sc.category_id = c.id WHERE sc.id=?
        ''', (subcategory_id,)).fetchone()
        if not subcat:
            return None

        diff_label = DIFF_LABELS.get(difficulty, difficulty)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO quizzes (title, category_id, subcategory_id, difficulty, description, time_limit) VALUES (?,?,?,?,?,?)",
            (f"{subcat['name']} - {diff_label}", subcat['cat_id'], subcategory_id, difficulty, "Quiz auto-généré", 0)
        )
        quiz_id = cur.lastrowid
        for q in questions:
            conn.execute("INSERT INTO quiz_questions VALUES (?,?)", (quiz_id, q['id']))
        return quiz_id

def get_or_create_daily_challenge():
    """Crée ou rafraîchit le défi du jour."""
    with get_db() as conn:
        today = datetime.now().date().isoformat()
        daily = conn.execute("SELECT * FROM quizzes WHERE is_daily_challenge=1 LIMIT 1").fetchone()

        if daily and str(daily['created_at'])[:10] == today:
            return daily['id']

        combo = conn.execute('''
            SELECT subcategory_id FROM questions
            WHERE difficulty='medium' AND subcategory_id IS NOT NULL
            GROUP BY subcategory_id HAVING COUNT(*) >= 2
            ORDER BY RANDOM() LIMIT 1
        ''').fetchone()

        if not combo:
            combo = conn.execute('''
                SELECT subcategory_id FROM questions
                WHERE subcategory_id IS NOT NULL
                GROUP BY subcategory_id HAVING COUNT(*) >= 1
                ORDER BY RANDOM() LIMIT 1
            ''').fetchone()

        if not combo:
            return None

        subcat_id = combo['subcategory_id']
        subcat = conn.execute('''
            SELECT sc.name, c.id as cat_id FROM subcategories sc
            JOIN categories c ON sc.category_id = c.id WHERE sc.id=?
        ''', (subcat_id,)).fetchone()
        if not subcat:
            return None

        best_diff = conn.execute('''
            SELECT difficulty FROM questions
            WHERE subcategory_id=? GROUP BY difficulty
            ORDER BY COUNT(*) DESC LIMIT 1
        ''', (subcat_id,)).fetchone()
        difficulty = best_diff['difficulty'] if best_diff else 'medium'

        old = conn.execute("SELECT id FROM quizzes WHERE is_daily_challenge=1").fetchone()
        if old:
            conn.execute("DELETE FROM quiz_questions WHERE quiz_id=?", (old['id'],))
            conn.execute("DELETE FROM quizzes WHERE id=?", (old['id'],))

        diff_label = DIFF_LABELS.get(difficulty, difficulty)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO quizzes (title, category_id, subcategory_id, difficulty, description, time_limit, is_daily_challenge) VALUES (?,?,?,?,?,?,?)",
            (f"⚡ Défi du Jour - {subcat['name']}", subcat['cat_id'], subcat_id, difficulty, "Relevez le défi quotidien !", 0, 1)
        )
        quiz_id = cur.lastrowid

        questions = conn.execute(
            "SELECT id FROM questions WHERE subcategory_id=? AND difficulty=?",
            (subcat_id, difficulty)
        ).fetchall()
        for q in questions:
            conn.execute("INSERT INTO quiz_questions VALUES (?,?)", (quiz_id, q['id']))
        return quiz_id

def refresh_all_quizzes():
    """Rafraîchit tous les quizzes auto-générés."""
    with get_db() as conn:
        combos = conn.execute('''
            SELECT DISTINCT subcategory_id, difficulty FROM questions
            WHERE subcategory_id IS NOT NULL
        ''').fetchall()
        for combo in combos:
            get_or_create_quiz(combo['subcategory_id'], combo['difficulty'])

# ==========================================
# DATABASE INIT
# ==========================================
def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('student', 'teacher')),
                current_level TEXT DEFAULT 'easy',
                xp INTEGER DEFAULT 0,
                streak INTEGER DEFAULT 0,
                last_activity DATE,
                avatar TEXT DEFAULT '🎮',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                icon TEXT DEFAULT '📚',
                color TEXT DEFAULT '#6c63ff'
            );
            CREATE TABLE IF NOT EXISTS subcategories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER,
                subcategory_id INTEGER,
                q_type TEXT NOT NULL CHECK(q_type IN ('mcq', 'fill_blank', 'match', 'code')),
                text TEXT NOT NULL,
                options TEXT,
                correct_answer TEXT NOT NULL,
                difficulty TEXT NOT NULL CHECK(difficulty IN ('easy', 'medium', 'hard')),
                explanation TEXT,
                points INTEGER DEFAULT 10,
                time_per_question INTEGER DEFAULT 30,
                FOREIGN KEY(category_id) REFERENCES categories(id),
                FOREIGN KEY(subcategory_id) REFERENCES subcategories(id)
            );
            CREATE TABLE IF NOT EXISTS quizzes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category_id INTEGER,
                subcategory_id INTEGER,
                difficulty TEXT NOT NULL,
                time_limit INTEGER DEFAULT 0,
                description TEXT,
                is_daily_challenge INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(category_id) REFERENCES categories(id),
                FOREIGN KEY(subcategory_id) REFERENCES subcategories(id)
            );
            CREATE TABLE IF NOT EXISTS quiz_questions (
                quiz_id INTEGER,
                question_id INTEGER,
                PRIMARY KEY(quiz_id, question_id),
                FOREIGN KEY(quiz_id) REFERENCES quizzes(id),
                FOREIGN KEY(question_id) REFERENCES questions(id)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                quiz_id INTEGER,
                score REAL,
                xp_earned INTEGER DEFAULT 0,
                time_taken INTEGER DEFAULT 0,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(quiz_id) REFERENCES quizzes(id)
            );
            CREATE TABLE IF NOT EXISTS answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER,
                question_id INTEGER,
                user_answer TEXT,
                is_correct INTEGER,
                marked_for_review INTEGER DEFAULT 0,
                time_spent INTEGER DEFAULT 0,
                FOREIGN KEY(attempt_id) REFERENCES attempts(id),
                FOREIGN KEY(question_id) REFERENCES questions(id)
            );
            CREATE TABLE IF NOT EXISTS badges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                icon TEXT NOT NULL,
                description TEXT,
                rarity TEXT DEFAULT 'common'
            );
            CREATE TABLE IF NOT EXISTS user_badges (
                user_id INTEGER,
                badge_id INTEGER,
                earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, badge_id)
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message TEXT NOT NULL,
                type TEXT DEFAULT 'info',
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS password_resets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                token TEXT NOT NULL,
                expires_at TIMESTAMP,
                used INTEGER DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS battles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                player1_id INTEGER,
                player2_id INTEGER,
                quiz_id INTEGER,
                attempt1_id INTEGER,
                attempt2_id INTEGER,
                status TEXT DEFAULT 'waiting',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(player1_id) REFERENCES users(id),
                FOREIGN KEY(player2_id) REFERENCES users(id),
                FOREIGN KEY(quiz_id) REFERENCES quizzes(id)
            );
            CREATE TABLE IF NOT EXISTS journal_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                attempt_id INTEGER,
                reflection_text TEXT,
                mood TEXT DEFAULT 'neutral',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(attempt_id) REFERENCES attempts(id)
            );
            CREATE TABLE IF NOT EXISTS team_battles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                quiz_id INTEGER,
                creator_id INTEGER,
                status TEXT DEFAULT 'waiting',
                max_members INTEGER DEFAULT 3,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(quiz_id) REFERENCES quizzes(id),
                FOREIGN KEY(creator_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS team_battle_members (
                team_battle_id INTEGER,
                user_id INTEGER,
                attempt_id INTEGER,
                PRIMARY KEY(team_battle_id, user_id),
                FOREIGN KEY(team_battle_id) REFERENCES team_battles(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
        ''')

        # Migrations pour les anciennes bases de données
        for col_sql in [
            "ALTER TABLE questions ADD COLUMN subcategory_id INTEGER",
            "ALTER TABLE quizzes ADD COLUMN subcategory_id INTEGER",
            "ALTER TABLE questions ADD COLUMN time_per_question INTEGER DEFAULT 30",
            "ALTER TABLE quizzes ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "ALTER TABLE battles ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        ]:
            try:
                conn.execute(col_sql)
            except:
                pass

        cur = conn.cursor()

        # --- Sous-catégories : toujours insérer si la table est vide ---
        if cur.execute("SELECT COUNT(*) FROM subcategories").fetchone()[0] == 0:
            subcats = [
                (1, "Notion d'algorithme"),
                (1, "Instructions de base"),
                (1, "Structures de contrôle de base séquentielle"),
                (1, "Structures de contrôle de base sélective"),
                (2, "Notion de programme et langages de programmation"),
                (2, "Transcription d'algorithmes"),
            ]
            for cat_id, name in subcats:
                conn.execute("INSERT INTO subcategories (category_id, name) VALUES (?,?)", (cat_id, name))

        if cur.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            conn.execute("INSERT INTO users (username, password_hash, role, avatar, xp) VALUES (?, ?, ?, ?, ?)",
                         ('teacher', generate_password_hash('password123'), 'teacher', '👨‍🏫', 9999))
            
            conn.execute("INSERT INTO categories (name, icon, color) VALUES ('Algorithmique', '⚙️', '#ff6b6b')")
            conn.execute("INSERT INTO categories (name, icon, color) VALUES ('Programmation', '🐍', '#4ecdc4')")

            conn.execute("DELETE FROM subcategories")
            subcats = [
                (1, "Notion d'algorithme"),
                (1, "Instructions de base"),
                (1, "Structures de contrôle de base séquentielle"),
                (1, "Structures de contrôle de base sélective"),
                (2, "Notion de programme et langages de programmation"),
                (2, "Transcription d'algorithmes"),
            ]
            for cat_id, name in subcats:
                conn.execute("INSERT INTO subcategories (category_id, name) VALUES (?,?)", (cat_id, name))

            conn.execute("INSERT INTO badges (name, icon, description, rarity) VALUES ('Premier Pas', '🌱', 'Complétez votre premier quiz', 'common')")
            conn.execute("INSERT INTO badges (name, icon, description, rarity) VALUES ('Intermédiaire', '🧠', 'Atteignez le niveau moyen', 'rare')")
            conn.execute("INSERT INTO badges (name, icon, description, rarity) VALUES ('Expert', '👑', 'Maîtrisez le niveau difficile', 'legendary')")
            conn.execute("INSERT INTO badges (name, icon, description, rarity) VALUES ('Perfectionniste', '💎', 'Obtenez 100% sur un quiz', 'legendary')")
            conn.execute("INSERT INTO badges (name, icon, description, rarity) VALUES ('Rapide', '⚡', 'Répondez en moins de 5s', 'rare')")
            conn.execute("INSERT INTO badges (name, icon, description, rarity) VALUES ('Streak x7', '🔥', '7 jours consécutifs', 'epic')")
            conn.execute("INSERT INTO badges (name, icon, description, rarity) VALUES ('Guerrier', '⚔️', 'Gagnez votre premier duel', 'rare')")

            seed_questions = [
                (1, 1, "Qu'est-ce qu'un algorithme ?", "mcq",
                 json.dumps(["Une suite finie d'instructions", "Un langage de programmation", "Un ordinateur", "Un système d'exploitation"]),
                 json.dumps("Une suite finie d'instructions"), "easy",
                 "Un algorithme est une suite finie et non ambiguë d'instructions.", 10, 30),
                (2, 6, "Complétez : print('Hello ____')", "fill_blank",
                 None, json.dumps("World"), "easy",
                 "Hello World est le programme traditionnel.", 10, 30),
                (1, 2, "Associez : 1.O(n) 2.O(1) -> A.Constant B.Linéaire (ex: 1B, 2A)", "match",
                 None, json.dumps("1B, 2A"), "medium",
                 "O(n) = linéaire, O(1) = constante.", 20, 45),
                (2, 6, "Écrivez une boucle for affichant 0 à 4.", "code",
                 None, json.dumps("for i in range(5):\n    print(i)"), "hard",
                 "range(5) génère les entiers de 0 à 4.", 30, 60),
                (1, 4, "Quelle structure utiliser pour LIFO ?", "mcq",
                 json.dumps(["Stack (Pile)", "Queue (File)", "Arbre binaire", "Graphe"]),
                 json.dumps("Stack (Pile)"), "medium",
                 "LIFO = Last In First Out = Pile.", 20, 45),
            ]
            for sq in seed_questions:
                conn.execute(
                    "INSERT INTO questions (category_id, subcategory_id, text, q_type, options, correct_answer, difficulty, explanation, points, time_per_question) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    sq
                )

            refresh_all_quizzes()

            demo_quizzes = conn.execute("SELECT id FROM quizzes WHERE is_daily_challenge=0 ORDER BY id LIMIT 2").fetchall()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if len(demo_quizzes) >= 1:
                conn.execute("INSERT INTO attempts (user_id, quiz_id, score, xp_earned, time_taken, started_at, completed_at) VALUES (?,?,100,50,45,?,?)",
                             (3, demo_quizzes[0]['id'], now, now))
            if len(demo_quizzes) >= 2:
                conn.execute("INSERT INTO attempts (user_id, quiz_id, score, xp_earned, time_taken, started_at, completed_at) VALUES (?,?,80,30,90,?,?)",
                             (4, demo_quizzes[1]['id'], now, now))
            conn.execute("INSERT INTO user_badges VALUES (3, 1, ?)", (now,))
            conn.execute("INSERT INTO user_badges VALUES (3, 4, ?)", (now,))
            conn.execute("INSERT INTO user_badges VALUES (4, 1, ?)", (now,))

        conn.commit()

def get_xp_level(xp):
    thresholds = [0, 100, 300, 600, 1000, 1500, 2200, 3000]
    names = ['Recrue', 'Apprenti', 'Initié', 'Développeur', 'Expert', 'Maître', 'Légende', 'Dieu du Code']
    for i, t in enumerate(thresholds):
        if xp < (thresholds[i+1] if i+1 < len(thresholds) else 99999):
            next_t = thresholds[i+1] if i+1 < len(thresholds) else thresholds[-1]+500
            progress = int(((xp - t) / (next_t - t)) * 100)
            return names[i], i+1, progress, next_t - xp
    return 'Dieu du Code', 8, 100, 0

class User(UserMixin):
    def __init__(self, user_dict):
        self.id = user_dict['id']
        self.username = user_dict['username']
        self.role = user_dict['role']
        self.current_level = user_dict['current_level']
        self.xp = user_dict['xp'] if user_dict['xp'] else 0
        self.streak = user_dict['streak'] if user_dict['streak'] else 0
        self.avatar = user_dict['avatar'] if user_dict['avatar'] else '🎮'
        self.last_activity = user_dict['last_activity']

@login_manager.user_loader
def load_user(user_id):
    try:
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return User(user) if user else None
    except Exception as e:
        logger.error(f"Error loading user: {e}")
        return None

def log_audit(user_id, action):
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO audit_logs (user_id, action) VALUES (?, ?)", (user_id, action))
    except Exception as e:
        logger.error(f"Audit log failed: {e}")

def add_notification(user_id, message, notif_type='info'):
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO notifications (user_id, message, type) VALUES (?, ?, ?)", (user_id, message, notif_type))
    except:
        pass

# ==========================================
# BASE TEMPLATE
# ==========================================
BASE_TEMPLATE = '''
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AlgoEdu — Plateforme Gamifiée</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/dracula.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/python/python.min.js"></script>
    <style>
        :root { --neon-cyan: #00f5ff; --neon-purple: #bf00ff; --neon-green: #00ff88; --neon-orange: #ff6b35; --neon-pink: #ff0080; --bg-dark: #080c14; --bg-card: #0d1526; --bg-card2: #111d35; --border-glow: rgba(0, 245, 255, 0.3); --text-primary: #e8f4ff; --text-muted: #6a8aad; }
        * { box-sizing: border-box; }
        body { background: var(--bg-dark); font-family: 'Rajdhani', sans-serif; color: var(--text-primary); min-height: 100vh; background-image: radial-gradient(ellipse at 20% 50%, rgba(0,245,255,0.04) 0%, transparent 50%), radial-gradient(ellipse at 80% 20%, rgba(191,0,255,0.04) 0%, transparent 50%); }
        .navbar { background: rgba(13, 21, 38, 0.95) !important; border-bottom: 1px solid var(--border-glow); backdrop-filter: blur(10px); padding: 0.6rem 0; }
        .navbar-brand { font-family: 'Orbitron', monospace; font-weight: 900; font-size: 1.3rem; color: var(--neon-cyan) !important; text-shadow: 0 0 20px rgba(0,245,255,0.5); letter-spacing: 2px; }
        .nav-link { color: var(--text-muted) !important; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; font-size: 0.8rem; transition: all 0.3s; padding: 0.5rem 1rem !important; }
        .nav-link:hover { color: var(--neon-cyan) !important; text-shadow: 0 0 10px var(--neon-cyan); }
        .xp-bar-nav { background: rgba(0,245,255,0.1); border: 1px solid rgba(0,245,255,0.2); border-radius: 20px; padding: 4px 12px; font-size: 0.75rem; color: var(--neon-cyan); font-family: 'Orbitron', monospace; }
        .card-neo { background: var(--bg-card); border: 1px solid rgba(0,245,255,0.15); border-radius: 12px; transition: all 0.3s ease; overflow: hidden; position: relative; }
        .card-neo::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; background: linear-gradient(90deg, transparent, var(--neon-cyan), transparent); opacity: 0; transition: opacity 0.3s; }
        .card-neo:hover::before { opacity: 1; }
        .card-neo:hover { border-color: rgba(0,245,255,0.4); transform: translateY(-2px); box-shadow: 0 8px 30px rgba(0,245,255,0.1); }
        .btn-neon { background: transparent; border: 1px solid var(--neon-cyan); color: var(--neon-cyan); font-family: 'Rajdhani', sans-serif; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; padding: 0.5rem 1.5rem; border-radius: 6px; transition: all 0.3s; font-size: 0.85rem; }
        .btn-neon:hover { background: var(--neon-cyan); color: var(--bg-dark); box-shadow: 0 0 25px rgba(0,245,255,0.4); }
        .btn-neon-purple { border-color: var(--neon-purple); color: var(--neon-purple); }
        .btn-neon-purple:hover { background: var(--neon-purple); color: white; box-shadow: 0 0 25px rgba(191,0,255,0.4); }
        .btn-neon-green { border-color: var(--neon-green); color: var(--neon-green); }
        .btn-neon-green:hover { background: var(--neon-green); color: var(--bg-dark); box-shadow: 0 0 25px rgba(0,255,136,0.4); }
        .btn-solid-cyan { background: linear-gradient(135deg, #00c8d7, #0099ff); border: none; color: white; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; padding: 0.6rem 2rem; border-radius: 6px; font-size: 0.85rem; }
        .xp-progress-container { background: rgba(255,255,255,0.05); border-radius: 10px; height: 8px; overflow: hidden; }
        .xp-progress-fill { height: 100%; background: linear-gradient(90deg, var(--neon-cyan), var(--neon-purple)); border-radius: 10px; transition: width 0.5s ease; box-shadow: 0 0 10px rgba(0,245,255,0.5); }
        .badge-card { background: var(--bg-card2); border-radius: 10px; padding: 12px; text-align: center; border: 1px solid rgba(255,255,255,0.05); transition: all 0.3s; }
        .badge-card:hover { border-color: var(--neon-purple); box-shadow: 0 0 15px rgba(191,0,255,0.2); }
        .badge-card .badge-icon { font-size: 2.5rem; display: block; margin-bottom: 5px; }
        .badge-legendary { border-color: rgba(255,215,0,0.4); }
        .badge-epic { border-color: rgba(191,0,255,0.4); }
        .badge-rare { border-color: rgba(0,112,255,0.4); }
        .diff-easy { color: var(--neon-green); border: 1px solid var(--neon-green); padding: 2px 10px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; letter-spacing: 1px; }
        .diff-medium { color: var(--neon-orange); border: 1px solid var(--neon-orange); padding: 2px 10px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; letter-spacing: 1px; }
        .diff-hard { color: var(--neon-pink); border: 1px solid var(--neon-pink); padding: 2px 10px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; letter-spacing: 1px; }
        .leaderboard-item { background: var(--bg-card2); border-radius: 10px; padding: 12px 16px; margin-bottom: 8px; border: 1px solid rgba(255,255,255,0.05); display: flex; align-items: center; transition: all 0.3s; }
        .leaderboard-item:hover { border-color: rgba(0,245,255,0.3); }
        .rank-1 { border-left: 3px solid #FFD700 !important; }
        .rank-2 { border-left: 3px solid #C0C0C0 !important; }
        .rank-3 { border-left: 3px solid #CD7F32 !important; }
        .rank-number { font-family: 'Orbitron', monospace; font-size: 0.8rem; color: var(--text-muted); min-width: 30px; }
        .timer-box { background: var(--bg-card2); border: 2px solid var(--neon-cyan); border-radius: 12px; padding: 8px 20px; font-family: 'Orbitron', monospace; font-size: 1.5rem; color: var(--neon-cyan); text-shadow: 0 0 10px var(--neon-cyan); display: inline-block; }
        .timer-box.danger { border-color: var(--neon-pink) !important; color: var(--neon-pink) !important; text-shadow: 0 0 10px var(--neon-pink) !important; animation: pulse 0.5s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
        .stat-card { background: var(--bg-card); border-radius: 12px; padding: 24px; border: 1px solid rgba(255,255,255,0.05); text-align: center; position: relative; overflow: hidden; }
        .stat-card::after { content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 3px; }
        .stat-cyan::after { background: var(--neon-cyan); } .stat-purple::after { background: var(--neon-purple); }
        .stat-green::after { background: var(--neon-green); } .stat-orange::after { background: var(--neon-orange); }
        .stat-number { font-family: 'Orbitron', monospace; font-size: 2.5rem; font-weight: 900; line-height: 1; }
        .stat-label { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 2px; color: var(--text-muted); margin-top: 5px; }
        .section-title { font-family: 'Orbitron', monospace; font-size: 1rem; letter-spacing: 3px; text-transform: uppercase; color: var(--text-muted); margin-bottom: 20px; padding-bottom: 10px; border-bottom: 1px solid rgba(255,255,255,0.05); }
        .form-control, .form-select { background: var(--bg-card2) !important; border: 1px solid rgba(0,245,255,0.2) !important; color: var(--text-primary) !important; border-radius: 8px; }
        .form-control:focus, .form-select:focus { border-color: var(--neon-cyan) !important; box-shadow: 0 0 0 3px rgba(0,245,255,0.1) !important; }
        .form-control::placeholder { color: var(--text-muted) !important; }
        .form-check-input { background-color: var(--bg-card2) !important; border-color: rgba(0,245,255,0.4) !important; }
        .form-check-input:checked { background-color: var(--neon-cyan) !important; border-color: var(--neon-cyan) !important; }
        label { color: var(--text-muted); font-size: 0.8rem; letter-spacing: 1px; text-transform: uppercase; }
        .alert-success { background: rgba(0,255,136,0.1) !important; border: 1px solid rgba(0,255,136,0.3) !important; color: var(--neon-green) !important; }
        .alert-danger { background: rgba(255,0,128,0.1) !important; border: 1px solid rgba(255,0,128,0.3) !important; color: var(--neon-pink) !important; }
        .alert-info { background: rgba(0,245,255,0.1) !important; border: 1px solid rgba(0,245,255,0.3) !important; color: var(--neon-cyan) !important; }
        .btn-close { filter: invert(1); }
        .question-card { background: var(--bg-card); border: 1px solid rgba(0,245,255,0.1); border-radius: 14px; padding: 28px; margin-bottom: 20px; transition: border-color 0.3s; }
        .question-card:hover { border-color: rgba(0,245,255,0.3); }
        .question-number { font-family: 'Orbitron', monospace; font-size: 0.7rem; color: var(--neon-purple); letter-spacing: 2px; margin-bottom: 8px; }
        .question-text { font-size: 1.15rem; font-weight: 600; color: var(--text-primary); margin-bottom: 20px; }
        .option-label { display: block; background: var(--bg-card2); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 12px 16px; margin-bottom: 8px; cursor: pointer; transition: all 0.2s; font-size: 0.95rem; }
        .option-label:hover { border-color: var(--neon-cyan); background: rgba(0,245,255,0.05); }
        input[type="radio"]:checked + .option-label, .option-selected { border-color: var(--neon-cyan) !important; background: rgba(0,245,255,0.1) !important; color: var(--neon-cyan); }
        .daily-badge { background: linear-gradient(135deg, rgba(255,107,53,0.2), rgba(255,0,128,0.2)); border: 1px solid var(--neon-orange); border-radius: 12px; padding: 20px; position: relative; overflow: hidden; }
        .answer-correct { border-color: rgba(0,255,136,0.5) !important; background: rgba(0,255,136,0.05) !important; }
        .answer-wrong { border-color: rgba(255,0,128,0.5) !important; background: rgba(255,0,128,0.05) !important; }
        .avatar-option { cursor: pointer; font-size: 2rem; padding: 10px; border-radius: 10px; border: 2px solid transparent; transition: all 0.2s; }
        .avatar-option:hover, .avatar-option.selected { border-color: var(--neon-cyan); background: rgba(0,245,255,0.1); }
        .table-neo { color: var(--text-primary) !important; }
        .table-neo thead th { color: var(--text-muted) !important; font-size: 0.75rem; letter-spacing: 2px; text-transform: uppercase; border-color: rgba(255,255,255,0.05) !important; background: transparent !important; }
        .table-neo tbody tr { border-color: rgba(255,255,255,0.03) !important; }
        .table-neo tbody tr:hover { background: rgba(0,245,255,0.03) !important; }
        .table-neo td { border-color: rgba(255,255,255,0.03) !important; padding: 12px 8px; vertical-align: middle; }
        ::-webkit-scrollbar { width: 6px; } ::-webkit-scrollbar-track { background: var(--bg-dark); } ::-webkit-scrollbar-thumb { background: rgba(0,245,255,0.2); border-radius: 3px; }
        @keyframes fadeInUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
        .fade-in { animation: fadeInUp 0.5s ease forwards; }
        .score-display { font-family: 'Orbitron', monospace; font-size: 5rem; font-weight: 900; text-align: center; background: linear-gradient(135deg, var(--neon-cyan), var(--neon-purple)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; line-height: 1; }
        .level-badge { background: linear-gradient(135deg, rgba(191,0,255,0.2), rgba(0,245,255,0.2)); border: 1px solid rgba(191,0,255,0.5); border-radius: 20px; padding: 4px 14px; font-family: 'Orbitron', monospace; font-size: 0.7rem; color: var(--neon-purple); letter-spacing: 1px; }
        .CodeMirror { background: var(--bg-card2) !important; color: var(--text-primary) !important; border-radius: 8px; height: 150px; font-size: 0.95rem; padding: 10px; }
        .CodeMirror-gutters { background: rgba(0,0,0,0.2) !important; border-right: 1px solid rgba(255,255,255,0.05) !important; }
        .streak-modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); z-index: 10000; display: flex; justify-content: center; align-items: center; animation: fadeInUp 0.3s ease; }
        .streak-modal-box { background: var(--bg-card); border: 2px solid var(--neon-orange); border-radius: 20px; padding: 40px; text-align: center; max-width: 400px; box-shadow: 0 0 50px rgba(255,107,53,0.3); }
        .vs-text { font-family: 'Orbitron', monospace; font-size: 4rem; font-weight: 900; color: var(--neon-pink); text-shadow: 0 0 30px var(--neon-pink); margin: 0 20px; }
        .battle-player-card { background: var(--bg-card); border-radius: 15px; padding: 30px; text-align: center; flex: 1; border: 1px solid rgba(255,255,255,0.1); }
        .battle-winner { border-color: var(--neon-green) !important; box-shadow: 0 0 30px rgba(0,255,136,0.2); }
        .battle-loser { opacity: 0.6; filter: grayscale(0.5); }
        .subcat-group { margin-bottom: 16px; }
        .subcat-header { font-size: 0.85rem; color: var(--text-muted); padding: 6px 12px; background: rgba(255,255,255,0.02); border-radius: 6px; margin-bottom: 8px; border-left: 2px solid var(--neon-purple); }
        .quiz-slot { background: var(--bg-card2); border: 1px solid rgba(255,255,255,0.05); border-radius: 8px; padding: 10px 14px; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; transition: all 0.2s; }
        .quiz-slot:hover { border-color: rgba(0,245,255,0.3); background: rgba(0,245,255,0.03); }
        .cat-section { margin-bottom: 28px; }
        .cat-section-title { font-family: 'Orbitron', monospace; font-size: 0.85rem; letter-spacing: 2px; margin-bottom: 14px; padding-bottom: 8px; border-bottom: 1px solid rgba(255,255,255,0.05); }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg">
        <div class="container-fluid px-4">
            <a class="navbar-brand" href="/"><span style="color: var(--neon-cyan);">ALGO</span><span style="color: var(--neon-purple);">EDU</span></a>
            {% if current_user.is_authenticated %}
            <button class="navbar-toggler border-0" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav"><span style="color: var(--neon-cyan);">☰</span></button>
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav me-auto">
                    {% if current_user.role == 'student' %}
                    <li class="nav-item"><a class="nav-link" href="{{ url_for('student_dashboard') }}">◈ Dashboard</a></li>
                    <li class="nav-item"><a class="nav-link" href="{{ url_for('student_quizzes') }}">◈ Quizzes</a></li>
                    <li class="nav-item"><a class="nav-link" href="{{ url_for('battle_menu') }}">⚔️ Battle</a></li>
                    <li class="nav-item"><a class="nav-link" href="{{ url_for('team_battle_menu') }}">🤝 Team</a></li>
                    <li class="nav-item"><a class="nav-link" href="{{ url_for('student_journal') }}">📓 Journal</a></li>
                    <li class="nav-item"><a class="nav-link" href="{{ url_for('leaderboard') }}">◈ Classement</a></li>
                    <li class="nav-item"><a class="nav-link" href="{{ url_for('profile') }}">◈ Profil</a></li>
                    {% elif current_user.role == 'teacher' %}
                    <li class="nav-item"><a class="nav-link" href="{{ url_for('teacher_dashboard') }}">◈ Analytics</a></li>
                    <li class="nav-item"><a class="nav-link" href="{{ url_for('manage_questions') }}">◈ Questions</a></li>
                    <li class="nav-item"><a class="nav-link" href="{{ url_for('manage_quizzes') }}">◈ Quizzes</a></li>
                    <li class="nav-item"><a class="nav-link" href="{{ url_for('manage_subcategories') }}">◈ Sous-catégories</a></li>
                    <li class="nav-item"><a class="nav-link" href="{{ url_for('export_csv') }}">◈ Export</a></li>
                    {% endif %}
                </ul>
                <div class="d-flex align-items-center gap-3">
                    {% if current_user.role == 'student' %}
                    <div class="xp-bar-nav">⚡ {{ current_user.xp }} XP</div>
                    <div class="d-none d-md-block" style="font-family: Orbitron, monospace; display: flex; align-items: center; gap: 5px;"><span>🔥</span><span style="color: var(--neon-orange); font-size: 1.1rem; font-weight: 900;">{{ current_user.streak }}</span></div>
                    {% endif %}
                    <a href="{{ url_for('profile') }}" style="color: var(--text-muted); text-decoration: none; font-size: 0.85rem;">{{ current_user.avatar }} {{ current_user.username }}</a>
                    <a href="{{ url_for('logout') }}" class="btn-neon btn btn-sm" style="padding: 4px 12px; font-size: 0.75rem;">Quitter</a>
                </div>
            </div>
            {% endif %}
        </div>
    </nav>
    <div class="container-fluid px-4 mt-4">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ 'danger' if category == 'error' else ('info' if category == 'info' else 'success') }} alert-dismissible fade show" style="border-radius: 10px;">{{ message }}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        {% block content %}{% endblock %}
    </div>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
    let sessionTime = 0;
    setInterval(() => { sessionTime++; if(sessionTime === 1800) alert('⏳ Vous jouez depuis 30 minutes. Pensez à faire une pause pour vos yeux et votre concentration !'); }, 1000);
    </script>
    {% block scripts %}{% endblock %}
</body>
</html>'''

app.jinja_env.loader = ChoiceLoader([DictLoader({'base.html': BASE_TEMPLATE}), app.jinja_env.loader])

def from_json(value):
    try: return json.loads(value)
    except (ValueError, TypeError): return value
app.jinja_env.filters['from_json'] = from_json

# ==========================================
# AUTH & ROUTES
# ==========================================
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('student_dashboard' if current_user.role == 'student' else 'teacher_dashboard'))
    return redirect(url_for('login'))

AVATARS = ['🎮','🐍','🦊','🐺','🦁','🐉','🤖','👾','🧙','🦅','🐬','🔥','⚡','🌟','🎯','🏆']

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        try:
            user_dict = None
            new_streak = 0
            with get_db() as conn:
                user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
                if user and check_password_hash(user['password_hash'], password):
                    today = datetime.now().date()
                    last = user['last_activity']
                    new_streak = user['streak'] or 0
                    if last:
                        last_date = datetime.strptime(str(last), '%Y-%m-%d').date()
                        diff = (today - last_date).days
                        if diff == 1: new_streak += 1
                        elif diff > 1: new_streak = 1
                    else: new_streak = 1
                    conn.execute("UPDATE users SET last_activity=?, streak=? WHERE id=?", (today.strftime('%Y-%m-%d'), new_streak, user['id']))
                    user_dict = dict(user)
            if user_dict:
                user_dict['streak'] = new_streak
                login_user(User(user_dict))
                log_audit(user_dict['id'], 'Login')
                flash(f'Bienvenue, {username} ! 🚀', 'success')
                return redirect(url_for('student_dashboard' if user_dict['role'] == 'student' else 'teacher_dashboard'))
            else:
                flash('Identifiants incorrects.', 'error')
        except Exception as e:
            logger.error(f"Login error: {e}")
            flash('Erreur serveur.', 'error')
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center" style="min-height: 85vh; align-items: center;"><div class="col-md-10 col-lg-8">
    <div class="text-center mb-5"><h1 style="font-family: Orbitron, monospace; font-size: 2.8rem; font-weight: 900;"><span style="color: var(--neon-cyan);">ALGO</span><span style="color: var(--neon-purple);">EDU</span></h1></div>
    <div class="row g-4">
        <div class="col-md-6"><div class="card-neo" style="padding: 36px;">
            <p class="section-title" style="text-align: center; border: none;">Connexion</p>
            <form method="POST">
                <div class="mb-4"><label class="mb-2">Pseudo</label><input type="text" name="username" class="form-control" required></div>
                <div class="mb-4"><label class="mb-2">Mot de passe</label><input type="password" name="password" class="form-control" required></div>
                <button type="submit" class="btn btn-solid-cyan w-100" style="padding: 13px;">Se connecter →</button>
            </form>
            <div class="mt-3 text-center"><a href="{{ url_for('forgot_password') }}" style="color: var(--text-muted); font-size: 0.85rem;">Mot de passe oublié ?</a></div>
            <div class="mt-3 text-center" style="color: var(--text-muted); font-size: 0.78rem;"><span style="color: var(--neon-cyan);"> </span></div>
        </div></div>
        <div class="col-md-6"><div class="card-neo" style="padding: 36px; border-color: rgba(191,0,255,0.3);">
            <p class="section-title" style="text-align: center; border: none; color: var(--neon-purple);">Nouveau ?</p>
            <div style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 24px; line-height: 1.8;">
                <div>⚡ <span style="color: var(--text-primary);">XP & Niveaux</span></div>
                <div>⚔️ <span style="color: var(--text-primary);">Battles multijoueurs</span></div>
                <div>🔥 <span style="color: var(--text-primary);">Streaks quotidiens</span></div>
            </div>
            <a href="{{ url_for('register') }}" class="btn w-100 btn-neon btn-neon-purple" style="padding: 13px; display: block; text-align: center; text-decoration: none;">Créer mon compte →</a>
        </div></div>
    </div>
</div></div>
{% endblock %}
''')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated: return redirect(url_for('index'))
    errors = {}
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        avatar = request.form.get('avatar', '🎮')
        if len(username) < 3: errors['username'] = 'Min 3 caractères.'
        if len(password) < 6: errors['password'] = 'Min 6 caractères.'
        if password != password2: errors['password2'] = 'Ne correspond pas.'
        if not errors:
            try:
                new_user_dict = None
                with get_db() as conn:
                    if conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
                        errors['username'] = 'Pris.'
                    else:
                        conn.execute("INSERT INTO users (username, password_hash, role, avatar) VALUES (?,?,?,?)", (username, generate_password_hash(password), 'student', avatar))
                        new_user_dict = dict(conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())
                if new_user_dict:
                    login_user(User(new_user_dict))
                    log_audit(new_user_dict['id'], 'Registered')
                    add_notification(new_user_dict['id'], '🎉 Bienvenue !', 'success')
                    flash('Compte créé !', 'success')
                    return redirect(url_for('student_dashboard'))
            except Exception as e:
                logger.error(f"Register error: {e}")
                flash('Erreur.', 'error')
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center" style="padding: 40px 0;"><div class="col-md-6 col-lg-5">
    <div class="card-neo" style="padding: 36px;">
        <p class="section-title" style="text-align: center; border: none;">Créer un compte</p>
        <form method="POST">
            <div class="mb-3"><label class="mb-2">Avatar</label><div style="display: flex; flex-wrap: wrap; gap: 6px;">{% for av in AVATARS %}<input type="radio" name="avatar" id="av_{{ loop.index }}" value="{{ av }}" style="display:none;" {% if loop.first %}checked{% endif %}><label for="av_{{ loop.index }}" class="avatar-option">{{ av }}</label>{% endfor %}</div></div>
            <div class="mb-3"><label>Pseudo</label><input type="text" name="username" class="form-control" value="{{ request.form.get('username', '') }}" required>{% if errors.username %}<div style="color: var(--neon-pink); font-size: 0.8rem;">{{ errors.username }}</div>{% endif %}</div>
            <div class="mb-3"><label>Mot de passe</label><input type="password" name="password" class="form-control" required>{% if errors.password %}<div style="color: var(--neon-pink); font-size: 0.8rem;">{{ errors.password }}</div>{% endif %}</div>
            <div class="mb-4"><label>Confirmer</label><input type="password" name="password2" class="form-control" required>{% if errors.password2 %}<div style="color: var(--neon-pink); font-size: 0.8rem;">{{ errors.password2 }}</div>{% endif %}</div>
            <button type="submit" class="btn btn-solid-cyan w-100">🚀 Créer</button>
        </form>
        <div class="mt-3 text-center"><a href="{{ url_for('login') }}" style="color: var(--neon-cyan); font-size: 0.85rem; text-decoration: none;">← Retour connexion</a></div>
    </div>
</div></div>
{% endblock %}
{% block scripts %}
<script>document.querySelectorAll('input[name="avatar"]').forEach(r => { r.addEventListener('change', function() { document.querySelectorAll('.avatar-option').forEach(l => l.classList.remove('selected')); document.querySelector('label[for="'+this.id+'"]').classList.add('selected'); }); if(r.checked) document.querySelector('label[for="'+r.id+'"]').classList.add('selected'); });</script>
{% endblock %}
''', errors=errors, AVATARS=AVATARS)

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if current_user.is_authenticated: return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        with get_db() as conn:
            user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            if user:
                token = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                expires = datetime.now() + timedelta(minutes=15)
                conn.execute("INSERT INTO password_resets (user_id, token, expires_at) VALUES (?, ?, ?)", (user['id'], token, expires.strftime('%Y-%m-%d %H:%M:%S')))
                return redirect(url_for('reset_password', token=token))
            else: flash('Pseudo introuvable.', 'error')
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center" style="min-height: 80vh; align-items: center;"><div class="col-md-5">
    <div class="card-neo" style="padding: 36px;">
        <p class="section-title" style="text-align: center; border: none;">🔐 Mot de passe oublié</p>
        <p style="color: var(--text-muted); text-align: center; font-size: 0.9rem;">Entrez votre pseudo pour générer un code.</p>
        <form method="POST"><div class="mb-4"><label class="mb-2">Pseudo</label><input type="text" name="username" class="form-control" required></div><button type="submit" class="btn btn-solid-cyan w-100">Générer le code</button></form>
    </div>
</div></div>
{% endblock %}
''')

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if current_user.is_authenticated: return redirect(url_for('index'))
    with get_db() as conn:
        reset = conn.execute("SELECT * FROM password_resets WHERE token=? AND used=0 AND expires_at > datetime('now')", (token,)).fetchone()
        if not reset: flash('Lien invalide ou expiré.', 'error'); return redirect(url_for('forgot_password'))
        if request.method == 'POST':
            pw = request.form.get('password', '')
            if len(pw) >= 6:
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), reset['user_id']))
                conn.execute("UPDATE password_resets SET used=1 WHERE id=?", (reset['id'],))
                flash('Mot de passe mis à jour !', 'success')
                return redirect(url_for('login'))
            else: flash('Minimum 6 caractères.', 'error')
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center" style="min-height: 80vh; align-items: center;"><div class="col-md-5">
    <div class="card-neo" style="padding: 36px;">
        <p class="section-title" style="text-align: center; border: none;">Nouveau mot de passe</p>
        <div class="alert alert-info" style="text-align: center; font-family: monospace; font-size: 1.2rem; letter-spacing: 3px;">{{ token }}</div>
        <form method="POST"><div class="mb-4"><label class="mb-2">Nouveau MDP</label><input type="password" name="password" class="form-control" required></div><button type="submit" class="btn btn-solid-cyan w-100">Mettre à jour</button></form>
    </div>
</div></div>
{% endblock %}
''', token=token)

@app.route('/logout')
@login_required
def logout():
    log_audit(current_user.id, 'Logout')
    logout_user()
    return redirect(url_for('login'))

# ==========================================
# STUDENT ROUTES
# ==========================================
@app.route('/student/dashboard')
@login_required
def student_dashboard():
    if current_user.role != 'student': return redirect(url_for('login'))
    with get_db() as conn:
        badges = conn.execute('SELECT b.name, b.icon, b.rarity FROM user_badges ub JOIN badges b ON ub.badge_id = b.id WHERE ub.user_id = ?', (current_user.id,)).fetchall()
        all_badges = conn.execute("SELECT * FROM badges").fetchall()
        attempts = conn.execute('SELECT a.id, q.title, a.score, a.completed_at, a.xp_earned, a.time_taken FROM attempts a JOIN quizzes q ON a.quiz_id = q.id WHERE a.user_id = ? ORDER BY a.completed_at DESC LIMIT 10', (current_user.id,)).fetchall()
        total_quizzes = len(attempts)
        avg_score = conn.execute("SELECT AVG(score) FROM attempts WHERE user_id=?", (current_user.id,)).fetchone()[0] or 0
        best_score = conn.execute("SELECT MAX(score) FROM attempts WHERE user_id=?", (current_user.id,)).fetchone()[0] or 0
        score_history = conn.execute('SELECT a.score, q.title FROM attempts a JOIN quizzes q ON a.quiz_id = q.id WHERE a.user_id=? ORDER BY a.completed_at DESC LIMIT 7', (current_user.id,)).fetchall()
        daily_id = get_or_create_daily_challenge()
        daily = conn.execute("SELECT * FROM quizzes WHERE id=?", (daily_id,)).fetchone() if daily_id else None

        show_streak_modal = False
        today = datetime.now().date()
        last = current_user.last_activity
        if last:
            last_date = datetime.strptime(str(last), '%Y-%m-%d').date()
            if last_date < today: show_streak_modal = True

    level_name, level_num, xp_progress, xp_needed = get_xp_level(current_user.xp)
    earned_ids = [b['name'] for b in badges]

    return render_template_string('''
{% extends "base.html" %}
{% block content %}
{% if show_streak_modal %}
<div class="streak-modal-overlay" id="streakModal">
    <div class="streak-modal-box">
        <div style="font-size: 4rem; margin-bottom: 10px;">🔥</div>
        <h3 style="font-family: Orbitron; color: var(--neon-orange); margin-bottom: 10px;">STREAK EN DANGER !</h3>
        <p style="color: var(--text-muted);">Vous n'avez pas fait de quiz aujourd'hui. Ne perdez pas vos {{ current_user.streak }} jours !</p>
        <a href="{{ url_for('student_quizzes') }}" class="btn btn-solid-cyan mt-3" style="background: var(--neon-orange);">Faire un quiz ⚡</a>
        <br><a href="#" onclick="document.getElementById('streakModal').style.display='none'" style="color: var(--text-muted); font-size: 0.8rem; margin-top: 15px; display: inline-block;">Plus tard</a>
    </div>
</div>
{% endif %}
<div class="row fade-in"><div class="col-12 mb-4">
    <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 16px;">
        <div style="display: flex; align-items: center; gap: 12px;">
            <span style="font-size: 2.5rem;">{{ current_user.avatar }}</span>
            <div><h2 style="font-family: Orbitron; font-size: 1.5rem; margin: 0;">{{ current_user.username|upper }}</h2><div class="level-badge">{{ level_name }} • LVL {{ level_num }}</div></div>
        </div>
        <div style="display: flex; gap: 20px;">
            <div class="text-center"><div style="font-family: Orbitron; font-size: 1.5rem; color: var(--neon-cyan);">{{ current_user.xp }}</div><div style="font-size: 0.7rem; color: var(--text-muted);">XP</div></div>
            <div class="text-center"><div style="font-family: Orbitron; font-size: 1.5rem; color: var(--neon-orange);">🔥 {{ current_user.streak }}</div><div style="font-size: 0.7rem; color: var(--text-muted);">JOURS</div></div>
            <div class="text-center"><div style="font-family: Orbitron; font-size: 1.5rem; color: var(--neon-green);">{{ "%.0f"|format(avg_score) }}%</div><div style="font-size: 0.7rem; color: var(--text-muted);">MOY.</div></div>
        </div>
    </div>
    <div class="mt-3"><div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-muted); margin-bottom: 6px;"><span>Niveau {{ level_num }}</span><span>{{ xp_needed }} XP restants</span></div><div class="xp-progress-container"><div class="xp-progress-fill" style="width: {{ xp_progress }}%;"></div></div></div>
</div></div>
<div class="row g-3 mb-4"><div class="col-6 col-md-3"><div class="stat-card stat-cyan"><div class="stat-number" style="color: var(--neon-cyan);">{{ total_quizzes }}</div><div class="stat-label">Quizzes</div></div></div><div class="col-6 col-md-3"><div class="stat-card stat-green"><div class="stat-number" style="color: var(--neon-green);">{{ badges|length }}</div><div class="stat-label">Badges</div></div></div><div class="col-6 col-md-3"><div class="stat-card stat-purple"><div class="stat-number" style="color: var(--neon-purple);">{{ "%.0f"|format(best_score) }}%</div><div class="stat-label">Meilleur</div></div></div><div class="col-6 col-md-3"><div class="stat-card stat-orange"><div class="stat-number" style="color: var(--neon-orange);">{{ current_user.streak }}</div><div class="stat-label">Streak</div></div></div></div>
<div class="row g-4">
    {% if daily %}<div class="col-12"><div class="daily-badge"><h5 style="color: var(--neon-orange); font-family: Orbitron; margin-bottom: 6px;">{{ daily.title }}</h5><p style="color: var(--text-muted); margin-bottom: 12px;">{{ daily.description }}</p><a href="{{ url_for('take_quiz', quiz_id=daily.id) }}" class="btn-neon btn" style="border-color: var(--neon-orange); color: var(--neon-orange);">Relever le Défi →</a></div></div>{% endif %}
    <div class="col-md-8"><div class="card-neo" style="padding: 24px;"><p class="section-title">📈 Progression</p>{% if score_history %}<canvas id="scoreChart" height="120"></canvas>{% else %}<div style="text-align: center; color: var(--text-muted); padding: 40px;">Faites un quiz !</div>{% endif %}</div></div>
    <div class="col-md-4"><div class="card-neo" style="padding: 24px; height: 100%;"><p class="section-title">🏅 Badges</p><div class="row g-2">{% for b in all_badges %}<div class="col-6"><div class="badge-card badge-{{ b.rarity }}" {% if b.name not in earned_ids %}style="opacity: 0.3; filter: grayscale(1);"{% endif %}><span class="badge-icon">{{ b.icon }}</span><div style="font-size: 0.7rem;">{{ b.name }}</div></div></div>{% endfor %}</div></div></div>
    <div class="col-12"><div class="card-neo" style="padding: 24px;"><p class="section-title">📋 Historique</p><table class="table table-neo"><thead><tr><th>Quiz</th><th>Score</th><th>XP</th><th>Temps</th><th>Date</th><th></th></tr></thead><tbody>{% for a in attempts %}<tr><td>{{ a.title }}</td><td style="font-family: Orbitron; color: {{ 'var(--neon-green)' if a.score >= 80 else 'var(--neon-orange)' }};">{{ a.score }}%</td><td style="color: var(--neon-cyan);">+{{ a.xp_earned }}</td><td style="color: var(--text-muted);">{% if a.time_taken %}{{ a.time_taken }}s{% endif %}</td><td style="color: var(--text-muted);">{{ a.completed_at[:10] if a.completed_at else '' }}</td><td><a href="{{ url_for('review_attempt', attempt_id=a.id) }}" class="btn-neon btn btn-sm" style="font-size: 0.7rem; padding: 3px 10px;">Revoir</a></td></tr>{% endfor %}</tbody></table></div></div>
</div>
{% endblock %}
{% block scripts %}
{% if score_history %}<script>const ctx = document.getElementById('scoreChart').getContext('2d'); new Chart(ctx, { type: 'line', data: { labels: [{% for s in score_history|reverse %}'{{ s.title[:15] }}'{% if not loop.last %},{% endif %}{% endfor %}], datasets: [{ label: 'Score', data: [{% for s in score_history|reverse %}{{ s.score }}{% if not loop.last %},{% endif %}{% endfor %}], borderColor: 'rgba(0,245,255,0.8)', backgroundColor: 'rgba(0,245,255,0.05)', pointBackgroundColor: '#00f5ff', fill: true, tension: 0.4 }] }, options: { plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#6a8aad' }, grid: { color: 'rgba(255,255,255,0.03)' } }, y: { min: 0, max: 100, ticks: { color: '#6a8aad' }, grid: { color: 'rgba(255,255,255,0.03)' } } } } });</script>{% endif %}
<script>setTimeout(() => { fetch('/api/notifications/read', { method: 'POST' }); }, 2000);</script>
{% endblock %}
''', badges=badges, all_badges=all_badges, attempts=attempts, total_quizzes=total_quizzes, avg_score=avg_score, best_score=best_score, score_history=score_history, daily=daily, show_streak_modal=show_streak_modal, level_name=level_name, level_num=level_num, xp_progress=xp_progress, xp_needed=xp_needed, earned_ids=earned_ids)

@app.route('/student/quizzes')
@login_required
def student_quizzes():
    if current_user.role != 'student': return redirect(url_for('login'))
    with get_db() as conn:
        combos = conn.execute('''
            SELECT sc.id as subcat_id, sc.name as subcat_name,
                   c.id as cat_id, c.name as cat_name, c.icon as cat_icon,
                   q.difficulty, COUNT(q.id) as q_count
            FROM subcategories sc
            JOIN categories c ON sc.category_id = c.id
            JOIN questions q ON q.subcategory_id = sc.id
            GROUP BY sc.id, q.difficulty
            ORDER BY c.name, sc.name,
                     CASE q.difficulty WHEN 'easy' THEN 1 WHEN 'medium' THEN 2 WHEN 'hard' THEN 3 END
        ''').fetchall()

        from collections import OrderedDict
        tree = OrderedDict()
        for combo in combos:
            cat_key = (combo['cat_id'], combo['cat_name'], combo['cat_icon'])
            if cat_key not in tree:
                tree[cat_key] = OrderedDict()
            if combo['subcat_id'] not in tree[cat_key]:
                tree[cat_key][combo['subcat_id']] = {'name': combo['subcat_name'], 'difficulties': []}
            tree[cat_key][combo['subcat_id']]['difficulties'].append({
                'difficulty': combo['difficulty'],
                'q_count': combo['q_count']
            })

        attempt_counts = {}
        for cat_key, subcats in tree.items():
            for subcat_id, subcat_data in subcats.items():
                for d in subcat_data['difficulties']:
                    quiz = conn.execute(
                        "SELECT id FROM quizzes WHERE subcategory_id=? AND difficulty=? AND is_daily_challenge=0",
                        (subcat_id, d['difficulty'])
                    ).fetchone()
                    if quiz:
                        cnt = conn.execute("SELECT COUNT(*) FROM attempts WHERE quiz_id=? AND user_id=?", (quiz['id'], current_user.id)).fetchone()[0]
                        attempt_counts[(subcat_id, d['difficulty'])] = cnt

    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-4">
    <div><h2 style="font-family: Orbitron; margin: 0;">QUIZZES</h2>
    <p style="color: var(--text-muted); font-size: 0.85rem; margin: 0;">Sélectionnez une sous-catégorie et une difficulté</p></div>
</div>
<div class="row">
    <div class="col-12">
        {% for (cat_id, cat_name, cat_icon), subcats in tree.items() %}
        <div class="cat-section">
            <div class="cat-section-title"><span style="font-size: 1.3rem;">{{ cat_icon }}</span> {{ cat_name }}</div>
            {% for subcat_id, subcat_data in subcats.items() %}
            <div class="subcat-group">
                <div class="subcat-header">📁 {{ subcat_data.name }}</div>
                {% for d in subcat_data.difficulties %}
                <form method="POST" action="{{ url_for('start_quiz') }}" style="display: inline;">
                    <input type="hidden" name="subcategory_id" value="{{ subcat_id }}">
                    <input type="hidden" name="difficulty" value="{{ d.difficulty }}">
                    <div class="quiz-slot">
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <span class="diff-{{ d.difficulty }}">{{ d.difficulty|upper }}</span>
                            <span style="font-size: 0.85rem;">{{ d.q_count }} question{{ 's' if d.q_count > 1 else '' }} • ⏱ Timer / question</span>
                        </div>
                        <div style="display: flex; align-items: center; gap: 10px;">
                            {% if attempt_counts.get((subcat_id, d.difficulty), 0) > 0 %}
                            <span style="font-size: 0.75rem; color: var(--text-muted);">✓ {{ attempt_counts.get((subcat_id, d.difficulty), 0) }}x</span>
                            {% else %}
                            <span style="font-size: 0.75rem; color: var(--neon-green);">Nouveau</span>
                            {% endif %}
                            <button type="submit" class="btn-neon btn btn-sm">Jouer →</button>
                        </div>
                    </div>
                </form>
                {% endfor %}
            </div>
            {% endfor %}
        </div>
        {% endfor %}
        {% if not tree %}
        <div class="card-neo" style="padding: 60px; text-align: center;">
            <div style="font-size: 3rem; margin-bottom: 16px;">📭</div>
            <p style="color: var(--text-muted);">Aucun quiz disponible pour le moment.</p>
        </div>
        {% endif %}
    </div>
</div>
{% endblock %}
''', tree=tree, attempt_counts=attempt_counts)

@app.route('/student/quiz/start', methods=['POST'])
@login_required
def start_quiz():
    if current_user.role != 'student': return redirect(url_for('login'))
    
    # GARDE-FOU TEMPS D'ECRAN (Max 45 min / jour)
    with get_db() as conn:
        today = datetime.now().strftime('%Y-%m-%d')
        daily_time = conn.execute("SELECT COALESCE(SUM(time_taken), 0) FROM attempts WHERE user_id=? AND started_at LIKE ?", (current_user.id, f"{today}%")).fetchone()[0]
        if daily_time > 2700: # 45 minutes en secondes
            flash('⏳ Garde-fou santé : Vous avez atteint 45 minutes de quiz aujourd\'hui. Reposez-vous, le code sera là demain !', 'error')
            return redirect(url_for('student_dashboard'))

    subcategory_id = int(request.form.get('subcategory_id'))
    difficulty = request.form.get('difficulty')
    quiz_id = get_or_create_quiz(subcategory_id, difficulty)
    if quiz_id:
        return redirect(url_for('take_quiz', quiz_id=quiz_id))
    else:
        flash('Aucune question disponible pour cette combinaison.', 'error')
        return redirect(url_for('student_quizzes'))

@app.route('/student/quiz/<int:quiz_id>', methods=['GET', 'POST'])
@login_required
def take_quiz(quiz_id):
    if current_user.role != 'student': return redirect(url_for('login'))
    with get_db() as conn:
        quiz = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        if not quiz:
            flash('Quiz introuvable.', 'error')
            return redirect(url_for('student_quizzes'))
        questions = conn.execute('''SELECT q.id, q.text, q.q_type, q.options, q.difficulty, q.correct_answer, q.explanation, q.points, q.time_per_question FROM quiz_questions qq JOIN questions q ON qq.question_id = q.id WHERE qq.quiz_id = ?''', (quiz_id,)).fetchall()
    
    # Récupération du contexte narratif
    narrative = get_narrative(quiz['subcategory_id'])

    if not questions: flash('Quiz vide.', 'error'); return redirect(url_for('student_quizzes'))

    if request.method == 'GET':
        return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center"><div class="col-lg-8">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 12px;">
        <div>
            <h3 style="font-family: Orbitron; margin: 0; font-size: 1.2rem;">{{ quiz.title }}</h3>
            <div class="daily-badge mt-2" style="padding: 15px; border: none; background: rgba(0,245,255,0.05);">
                <div style="font-size: 0.9rem; font-weight: 600;">{{ narrative[1] }}</div>
            </div>
        </div>
        <div><div class="timer-box" id="timer">00:30</div><div class="timer-progress" style="width: 120px; margin: 6px auto 0; background: rgba(0,245,255,0.1); border-radius: 4px; height: 6px; overflow: hidden;"><div class="timer-progress-fill" id="timerProgressFill" style="width: 100%; height: 100%; background: var(--neon-cyan); border-radius: 4px; transition: width 1s linear;"></div></div></div>
    </div>
    <div class="xp-progress-container mb-3"><div class="xp-progress-fill" id="progress-fill" style="width: 0%;"></div></div>
    <form method="POST" id="quizForm" novalidate><input type="hidden" name="time_taken" id="time_taken" value="0">
        {% for q in questions %}
        <div class="question-card" id="question_{{ loop.index0 }}" style="{% if not loop.first %}display: none;{% endif %}">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <div class="question-number">QUESTION {{ loop.index }} / {{ questions|length }}</div>
                <span style="color: var(--neon-orange); font-size: 0.75rem; font-family: Orbitron;">⏱ {{ q.time_per_question or 30 }}s</span>
            </div>
            <div class="question-text">{{ q.text }}</div>
            <div class="d-flex gap-2 mb-3"><span class="diff-{{ q.difficulty }}">{{ q.difficulty|upper }}</span><span style="color: var(--neon-cyan); font-size: 0.75rem; font-family: Orbitron;">+{{ q.points }} pts</span></div>
            {% if q.q_type == 'mcq' %}{% set opts = q.options|from_json %}{% for opt in opts %}<div><input type="radio" name="ans_{{ q.id }}" id="opt_{{ q.id }}_{{ loop.index }}" value="{{ opt }}" style="display:none;"><label for="opt_{{ q.id }}_{{ loop.index }}" class="option-label">{{ opt }}</label></div>{% endfor %}
            {% elif q.q_type == 'code' %}<textarea class="form-control code-editor" name="ans_{{ q.id }}" id="ans_{{ q.id }}" rows="4" placeholder="Code..." style="display: none;"></textarea>
            {% else %}<textarea class="form-control" name="ans_{{ q.id }}" rows="3" placeholder="Réponse..."></textarea>{% endif %}
        </div>
        {% endfor %}
        <div style="display: flex; justify-content: space-between; margin: 20px 0 40px;">
            <button type="button" class="btn-neon btn" id="prevBtn" style="display: none;" onclick="prevQuestion()">← Précédent</button>
            <button type="button" class="btn-neon btn" id="nextBtn" onclick="nextQuestion()">Suivant →</button>
            <button type="submit" class="btn btn-solid-cyan" id="submitBtn" style="display: none; padding: 16px; font-size: 1rem;">✓ Soumettre</button>
        </div>
    </form>
</div></div>
{% endblock %}
{% block scripts %}
<script>
const questionTimes = [{% for q in questions %}{{ q.time_per_question or 30 }}{% if not loop.last %},{% endif %}{% endfor %}];
const totalQuestions = {{ questions|length }};
let currentQ = 0, timeLeft = questionTimes[0], startTime = Date.now(), timerInterval = null;
const timerEl = document.getElementById('timer'), timerProgressFill = document.getElementById('timerProgressFill'), progressFill = document.getElementById('progress-fill');
const prevBtn = document.getElementById('prevBtn'), nextBtn = document.getElementById('nextBtn'), submitBtn = document.getElementById('submitBtn');
function startTimer() { clearInterval(timerInterval); timeLeft = questionTimes[currentQ]; updateTimerDisplay(); timerInterval = setInterval(() => { timeLeft--; updateTimerDisplay(); if (timeLeft <= 0) { clearInterval(timerInterval); if (currentQ < totalQuestions - 1) nextQuestion(); else submitQuiz(); } }, 1000); }
function updateTimerDisplay() { const m = String(Math.floor(timeLeft/60)).padStart(2,'0'), s = String(timeLeft%60).padStart(2,'0'); timerEl.textContent = m+':'+s; const pct = (timeLeft / questionTimes[currentQ]) * 100; timerProgressFill.style.width = pct + '%'; if(timeLeft <= 10){ timerEl.classList.add('danger'); timerProgressFill.style.background='var(--neon-pink)'; } else { timerEl.classList.remove('danger'); timerProgressFill.style.background='var(--neon-cyan)'; } }
function showQuestion(i) { document.querySelectorAll('.question-card').forEach((c,idx) => { c.style.display = idx === i ? 'block' : 'none'; if(idx===i) c.querySelectorAll('.code-editor').forEach(t => { if(t.cmInstance) setTimeout(()=>t.cmInstance.refresh(),10); }); }); progressFill.style.width = ((i+1)/totalQuestions*100)+'%'; prevBtn.style.display = i > 0 ? 'inline-block' : 'none'; nextBtn.style.display = i < totalQuestions - 1 ? 'inline-block' : 'none'; submitBtn.style.display = i === totalQuestions - 1 ? 'inline-block' : 'none'; currentQ = i; startTimer(); }
function nextQuestion() { 
    // PAUSE METACOGNITIVE TOUTES LES 2 QUESTIONS
    if((currentQ + 1) % 2 === 0 && currentQ < totalQuestions - 1) {
        prompt("⏸️ Pause réflexion : Quelle stratégie as-tu utilisé pour les dernières questions ? (Prends le temps d'y réfléchir, tu pourras le noter dans ton Journal de bord)");
    }
    if(currentQ < totalQuestions - 1) showQuestion(currentQ + 1); 
}
function prevQuestion() { if(currentQ > 0) showQuestion(currentQ - 1); }
function submitQuiz() { clearInterval(timerInterval); document.getElementById('time_taken').value = Math.round((Date.now() - startTime) / 1000); document.querySelectorAll('.code-editor').forEach(t => { if(t.cmInstance) t.cmInstance.save(); }); document.getElementById('quizForm').submit(); }
document.getElementById('quizForm').addEventListener('submit', () => { clearInterval(timerInterval); document.getElementById('time_taken').value = Math.round((Date.now() - startTime) / 1000); document.querySelectorAll('.code-editor').forEach(t => { if(t.cmInstance) t.cmInstance.save(); }); });
document.querySelectorAll('input[type="radio"]').forEach(r => { r.addEventListener('change', function() { document.querySelectorAll('input[name="'+this.name+'"]').forEach(x => document.querySelector('label[for="'+x.id+'"]').classList.remove('option-selected')); document.querySelector('label[for="'+this.id+'"]').classList.add('option-selected'); }); });
document.querySelectorAll('.code-editor').forEach(t => { let cm = CodeMirror.fromTextArea(t, { lineNumbers: true, theme: "dracula", mode: "python" }); t.cmInstance = cm; });
document.addEventListener('keydown', function(e) { if(e.target.tagName==='TEXTAREA'||e.target.tagName==='INPUT') return; if(e.key==='ArrowRight') nextQuestion(); if(e.key==='ArrowLeft') prevQuestion(); });
startTimer();
</script>
{% endblock %}
''', quiz=quiz, questions=questions, narrative=narrative)

    elif request.method == 'POST':
        try:
            score = 0; total = len(questions); now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            time_taken = int(request.form.get('time_taken', 0))
            attempt_id = None; final_score = 0; xp_earned = 0
            notif_level = ""; notif_badge = ""
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("INSERT INTO attempts (user_id, quiz_id, score, started_at, completed_at, time_taken) VALUES (?,?,?,?,?,?)", (current_user.id, quiz_id, 0, now, now, time_taken))
                attempt_id = cur.lastrowid
                for q in questions:
                    user_ans = request.form.get(f"ans_{q['id']}", '')
                    correct = json.loads(q['correct_answer'])
                    is_correct = 1 if (user_ans.strip().lower() == str(correct).strip().lower()) else 0
                    score += is_correct
                    cur.execute("INSERT INTO answers (attempt_id, question_id, user_answer, is_correct, time_spent) VALUES (?,?,?,?,?)", (attempt_id, q['id'], user_ans, is_correct, q['time_per_question'] or 30))
                final_score = (score / total) * 100 if total > 0 else 0
                xp_earned = 50 if final_score >= 80 else (25 if final_score >= 60 else (10 if final_score > 0 else 0))
                if time_taken < 60 and final_score >= 80: xp_earned += 20
                conn.execute("UPDATE attempts SET score=?, xp_earned=? WHERE id=?", (final_score, xp_earned, attempt_id))
                conn.execute("UPDATE users SET xp = xp + ? WHERE id=?", (xp_earned, current_user.id))
                if final_score >= 80:
                    new_level = {'easy': 'medium', 'medium': 'hard'}.get(current_user.current_level)
                    if new_level:
                        conn.execute("UPDATE users SET current_level=? WHERE id=?", (new_level, current_user.id))
                        current_user.current_level = new_level
                        notif_level = f'🎉 Niveau débloqué : {new_level.upper()} !'
                if final_score == 100: conn.execute("INSERT OR IGNORE INTO user_badges VALUES (?,4,?)", (current_user.id, now)); notif_badge = '💎 Badge "Perfectionniste" débloqué !'
                if final_score > 0: conn.execute("INSERT OR IGNORE INTO user_badges VALUES (?,1,?)", (current_user.id, now))
                if current_user.streak >= 7: conn.execute("INSERT OR IGNORE INTO user_badges VALUES (?,6,?)", (current_user.id, now))
            log_audit(current_user.id, f'Completed quiz {quiz_id} score={final_score}%')
            if notif_level: add_notification(current_user.id, notif_level, 'success')
            if notif_badge: add_notification(current_user.id, notif_badge, 'success')
            return redirect(url_for('result_screen', attempt_id=attempt_id))
        except Exception as e:
            logger.error(f"Quiz submission error: {e}")
            flash(f'Erreur: {e}', 'error')
            return redirect(url_for('take_quiz', quiz_id=quiz_id))

@app.route('/student/result/<int:attempt_id>')
@login_required
def result_screen(attempt_id):
    with get_db() as conn:
        attempt = conn.execute("SELECT a.*, q.title FROM attempts a JOIN quizzes q ON a.quiz_id=q.id WHERE a.id=? AND a.user_id=?", (attempt_id, current_user.id)).fetchone()
        if not attempt: return redirect(url_for('student_dashboard'))
        correct_count = conn.execute("SELECT COUNT(*) FROM answers WHERE attempt_id=? AND is_correct=1", (attempt_id,)).fetchone()[0]
        answer_count = conn.execute("SELECT COUNT(*) FROM answers WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center" style="min-height: 80vh; align-items: center;"><div class="col-md-6 text-center"><div class="card-neo" style="padding: 48px;">
    <div style="font-size: 4rem; margin-bottom: 16px;">{% if attempt.score >= 80 %}🏆{% elif attempt.score >= 60 %}⚡{% else %}📚{% endif %}</div>
    <p style="font-family: Orbitron; font-size: 0.8rem; color: var(--text-muted); letter-spacing: 3px;">QUIZ TERMINÉ</p>
    <div class="score-display" id="scoreNum">0%</div>
    <p style="color: var(--text-muted); margin: 16px 0;">{{ correct_count }} / {{ answer_count }} correctes</p>
    <div style="display: flex; justify-content: center; gap: 24px; margin: 24px 0;"><div><div style="font-family: Orbitron; font-size: 1.2rem; color: var(--neon-cyan);">+{{ attempt.xp_earned }}</div><div style="font-size: 0.7rem; color: var(--text-muted);">XP</div></div><div><div style="font-family: Orbitron; font-size: 1.2rem; color: var(--neon-orange);">{{ attempt.time_taken }}s</div><div style="font-size: 0.7rem; color: var(--text-muted);">TEMPS</div></div></div>
    <div style="display: flex; gap: 12px; justify-content: center; flex-wrap: wrap;">
        <a href="{{ url_for('review_attempt', attempt_id=attempt.id) }}" class="btn-neon btn">Détails</a>
        <a href="{{ url_for('student_quizzes') }}" class="btn btn-solid-cyan">Continuer →</a>
    </div>
    <form method="POST" action="{{ url_for('save_reflection') }}" style="margin-top: 25px; text-align: left;">
        <input type="hidden" name="attempt_id" value="{{ attempt.id }}">
        <label style="margin-bottom: 5px; display:block;">📓 Ajouter une réflexion au Journal de bord</label>
        <textarea name="reflection" class="form-control" rows="2" placeholder="Qu'avez-vous appris ? Quelles ont été vos difficultés ?"></textarea>
        <button type="submit" class="btn btn-neon-purple btn-sm mt-2" style="border:1px solid var(--neon-purple); color:var(--neon-purple);">Sauvegarder</button>
    </form>
</div></div></div>
{% endblock %}
{% block scripts %}
<script>const target = {{ attempt.score }}; let cur = 0; const el = document.getElementById('scoreNum'); const intv = setInterval(() => { cur = Math.min(cur + 2, target); el.textContent = cur.toFixed(0) + '%'; if(cur >= target) clearInterval(intv); }, 20);</script>
{% endblock %}
''', attempt=attempt, answer_count=answer_count, correct_count=correct_count)

@app.route('/student/review/<int:attempt_id>')
@login_required
def review_attempt(attempt_id):
    with get_db() as conn:
        attempt = conn.execute("SELECT a.*, q.title, q.subcategory_id FROM attempts a JOIN quizzes q ON a.quiz_id=q.id WHERE a.id=? AND a.user_id=?", (attempt_id, current_user.id)).fetchone()
        if not attempt: return redirect(url_for('student_dashboard'))
        answers = conn.execute('''SELECT ans.*, q.text, q.correct_answer, q.q_type, q.explanation FROM answers ans JOIN questions q ON ans.question_id = q.id WHERE ans.attempt_id = ?''', (attempt_id,)).fetchall()
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center"><div class="col-lg-8">
    <h3 style="font-family: Orbitron; margin-bottom: 24px;">Révision : {{ attempt.title }} <span style="color: {{ 'var(--neon-green)' if attempt.score >= 80 else 'var(--neon-pink)' }};">({{ attempt.score }}%)</span></h3>
    {% for a in answers %}
    <div class="question-card">
        <div class="question-text">{{ a.text }}</div>
        <div style="{% if a.is_correct %}background: rgba(0,255,136,0.05); border: 1px solid rgba(0,255,136,0.2);{% else %}background: rgba(255,0,128,0.05); border: 1px solid rgba(255,0,128,0.2);{% endif %} border-radius: 8px; padding: 15px; margin-top: 12px;">
            {% if a.is_correct %}
                <div style="color: var(--neon-green); font-weight: 700; margin-bottom: 5px;">✅ SUCCÈS : Mission accomplie ! Le code s'exécute.</div>
                <div style="color: var(--text-muted); font-size: 0.9rem;">Votre sortie : {{ a.user_answer }}</div>
            {% else %}
                <div style="color: var(--neon-pink); font-weight: 700; margin-bottom: 5px;">❌ ÉCHEC DU PROTOCOLE : Le système a rejeté l'instruction.</div>
                <div style="color: var(--text-muted); font-size: 0.85rem; margin-bottom: 8px;">Votre tentative : <span style="color: var(--neon-pink);">{{ a.user_answer or '(vide)' }}</span> a provoqué un bug dans la matrice.</div>
                <div style="background: rgba(0,245,255,0.05); padding: 8px; border-radius: 4px; font-size: 0.9rem;">🔧 <strong>Correction requise :</strong> Le bon algorithme était <span style="color: var(--neon-cyan);">{{ a.correct_answer }}</span></div>
            {% endif %}
        </div>
        {% if a.explanation %}<div style="margin-top: 12px; padding: 12px; background: rgba(0,245,255,0.03); border-left: 3px solid var(--neon-cyan); border-radius: 4px;"><span style="color: var(--neon-cyan); font-size: 0.75rem;">💡 </span><span style="color: var(--text-muted);">{{ a.explanation }}</span></div>{% endif %}
    </div>
    {% endfor %}
    <a href="{{ url_for('student_dashboard') }}" class="btn-neon btn mt-3 mb-5">← Retour</a>
</div></div>
{% endblock %}
''', attempt=attempt, answers=answers)

# ==========================================
# JOURNAL DE BORD & METACOGNITION
# ==========================================
@app.route('/student/journal')
@login_required
def student_journal():
    with get_db() as conn:
        entries = conn.execute('''SELECT j.*, q.title FROM journal_entries j LEFT JOIN attempts a ON j.attempt_id=a.id LEFT JOIN quizzes q ON a.quiz_id=q.id WHERE j.user_id=? ORDER BY j.created_at DESC LIMIT 30''', (current_user.id,)).fetchall()
        patterns = conn.execute('''SELECT q.text, COUNT(a.id) as fails FROM answers a JOIN questions q ON a.question_id=q.id JOIN attempts att ON a.attempt_id=att.id WHERE a.is_correct=0 AND att.user_id=? GROUP BY q.text ORDER BY fails DESC LIMIT 5''', (current_user.id,)).fetchall()
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center"><div class="col-lg-8">
    <h2 style="font-family: Orbitron; margin-bottom: 20px;">📓 Journal de Bord</h2>
    <div class="card-neo mb-4" style="padding: 24px;">
        <p class="section-title">🧠 Mes réflexions post-quiz</p>
        {% for e in entries %}
        <div style="border-left: 3px solid var(--neon-purple); padding: 10px 15px; margin-bottom: 10px; background: var(--bg-card2); border-radius: 0 8px 8px 0;">
            <div style="font-size: 0.75rem; color: var(--text-muted);">{{ e.created_at[:16] }} — {{ e.title or 'Quiz supprimé' }}</div>
            <div style="margin-top: 5px;">{{ e.reflection_text }}</div>
        </div>
        {% else %}<p style="color: var(--text-muted); text-align: center;">Faites un quiz pour commencer à réfléchir sur votre apprentissage !</p>{% endfor %}
    </div>
    <div class="card-neo" style="padding: 24px;">
        <p class="section-title">🔍 Patterns d'erreurs récurrents</p>
        {% for p in patterns %}
        <div style="margin-bottom: 8px; display: flex; justify-content: space-between; background: rgba(255,0,128,0.05); padding: 8px 12px; border-radius: 6px;">
            <span>{{ p.text[:80] }}...</span><span style="color: var(--neon-pink); font-family: Orbitron;">{{ p.fails }}x</span>
        </div>
        {% else %}<p style="color: var(--text-muted); text-align: center;">Aucune erreur récurrente pour le moment !</p>{% endfor %}
    </div>
</div></div>
{% endblock %}
''', entries=entries, patterns=patterns)

@app.route('/student/save_reflection', methods=['POST'])
@login_required
def save_reflection():
    attempt_id = request.form.get('attempt_id')
    reflection = request.form.get('reflection', '').strip()
    if attempt_id and reflection:
        with get_db() as conn:
            conn.execute("INSERT INTO journal_entries (user_id, attempt_id, reflection_text) VALUES (?, ?, ?)", (current_user.id, attempt_id, reflection))
    return redirect(url_for('student_dashboard'))

# ==========================================
# PROFILE
# ==========================================
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        avatar = request.form.get('avatar', current_user.avatar)
        if avatar in AVATARS:
            with get_db() as conn:
                conn.execute("UPDATE users SET avatar=? WHERE id=?", (avatar, current_user.id))
            current_user.avatar = avatar
            flash('Avatar mis à jour !', 'success')
        return redirect(url_for('profile'))
    with get_db() as conn:
        cat_stats = conn.execute('''
            SELECT c.name, c.icon, COUNT(DISTINCT a.id) as attempts, COALESCE(AVG(a.score), 0) as avg_score
            FROM categories c
            LEFT JOIN subcategories sc ON sc.category_id = c.id
            LEFT JOIN questions q ON q.subcategory_id = sc.id
            LEFT JOIN quiz_questions qq ON q.id = qq.question_id
            LEFT JOIN quizzes qz ON qq.quiz_id = qz.id
            LEFT JOIN attempts a ON qz.id = a.quiz_id AND a.user_id = ?
            GROUP BY c.id
        ''', (current_user.id,)).fetchall()
        total_time = conn.execute("SELECT SUM(time_taken) FROM attempts WHERE user_id=?", (current_user.id,)).fetchone()[0] or 0
    level_name, level_num, xp_progress, xp_needed = get_xp_level(current_user.xp)
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center"><div class="col-lg-8">
    <div class="card-neo mb-4" style="padding: 30px; text-align: center;">
        <span style="font-size: 5rem;">{{ current_user.avatar }}</span>
        <h2 style="font-family: Orbitron; margin: 10px 0;">{{ current_user.username|upper }}</h2>
        <div class="level-badge d-inline-block mb-3">{{ level_name }} • LVL {{ level_num }}</div>
        <div style="max-width: 400px; margin: 0 auto;"><div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-muted); margin-bottom: 6px;"><span>{{ current_user.xp }} XP</span><span>Prochain: +{{ xp_needed }} XP</span></div><div class="xp-progress-container"><div class="xp-progress-fill" style="width: {{ xp_progress }}%;"></div></div></div>
    </div>
    <div class="card-neo mb-4" style="padding: 24px;"><p class="section-title">Changer d'Avatar</p><form method="POST"><div style="display: flex; flex-wrap: wrap; gap: 8px; justify-content: center;">{% for av in AVATARS %}<input type="radio" name="avatar" id="pav_{{ loop.index }}" value="{{ av }}" style="display:none;" {% if av == current_user.avatar %}checked{% endif %}><label for="pav_{{ loop.index }}" class="avatar-option {% if av == current_user.avatar %}selected{% endif %}">{{ av }}</label>{% endfor %}</div><button type="submit" class="btn btn-neon btn-sm mt-3">Sauvegarder</button></form></div>
    <div class="row g-3 mb-4"><div class="col-4"><div class="stat-card stat-cyan"><div class="stat-number" style="color: var(--neon-cyan); font-size: 1.5rem;">{{ "%d:%02d"|format(total_time // 60, total_time % 60) }}</div><div class="stat-label">Temps de jeu</div></div></div><div class="col-4"><div class="stat-card stat-purple"><div class="stat-number" style="color: var(--neon-purple); font-size: 1.5rem;">{{ current_user.xp }}</div><div class="stat-label">XP Total</div></div></div><div class="col-4"><div class="stat-card stat-green"><div class="stat-number" style="color: var(--neon-green); font-size: 1.5rem;">{{ current_user.streak }}</div><div class="stat-label">Streak</div></div></div></div>
    <div class="card-neo" style="padding: 24px;"><p class="section-title">Stats par Catégorie</p><table class="table table-neo"><thead><tr><th>Catégorie</th><th>Tentatives</th><th>Score Moyen</th></tr></thead><tbody>{% for c in cat_stats %}<tr><td>{{ c.icon }} {{ c.name }}</td><td style="font-family: Orbitron; color: var(--neon-cyan);">{{ c.attempts }}</td><td style="font-family: Orbitron; color: {{ 'var(--neon-green)' if c.avg_score >= 80 else 'var(--neon-orange)' }};">{{ "%.0f"|format(c.avg_score) }}%</td></tr>{% endfor %}</tbody></table></div>
</div></div>
{% endblock %}
{% block scripts %}
<script>document.querySelectorAll('input[name="avatar"]').forEach(r => { r.addEventListener('change', function() { document.querySelectorAll('.avatar-option').forEach(l => l.classList.remove('selected')); document.querySelector('label[for="'+this.id+'"]').classList.add('selected'); }); });</script>
{% endblock %}
''', cat_stats=cat_stats, total_time=total_time, level_name=level_name, level_num=level_num, xp_progress=xp_progress, xp_needed=xp_needed, AVATARS=AVATARS)

# ==========================================
# BATTLE MODE & TEAM BATTLE
# ==========================================
@app.route('/battle')
@login_required
def battle_menu():
    if current_user.role != 'student': return redirect(url_for('login'))
    with get_db() as conn:
        quizzes = conn.execute("SELECT q.id, q.title, q.difficulty FROM quizzes q WHERE q.is_daily_challenge=0").fetchall()
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center"><div class="col-md-6">
    <div class="text-center mb-5"><h2 style="font-family: Orbitron; color: var(--neon-pink);">⚔️ MODE BATTLE</h2><p style="color: var(--text-muted);">Affrontez un autre apprenant !</p></div>
    <div class="card-neo mb-4" style="padding: 24px;"><p class="section-title">Créer un Salon</p><form method="POST" action="{{ url_for('create_battle') }}"><select name="quiz_id" class="form-select mb-3" required>{% for q in quizzes %}<option value="{{ q.id }}">{{ q.title }} ({{ q.difficulty }})</option>{% endfor %}</select><button type="submit" class="btn w-100" style="padding: 13px; background: linear-gradient(135deg, #bf00ff, #ff0080); border: none; color: white; font-weight: 700; letter-spacing: 1px; border-radius: 6px;">Générer un code</button></form></div>
    <div class="card-neo" style="padding: 24px;"><p class="section-title">Rejoindre</p><form method="POST" action="{{ url_for('join_battle') }}"><input type="text" name="code" class="form-control mb-3" placeholder="Code à 4 chiffres" maxlength="4" required style="text-align: center; font-family: Orbitron; font-size: 1.5rem; letter-spacing: 10px;"><button type="submit" class="btn btn-neon btn-neon-purple w-100">Rejoindre →</button></form></div>
</div></div>
{% endblock %}
''', quizzes=quizzes)

@app.route('/battle/create', methods=['POST'])
@login_required
def create_battle():
    code = ''.join(random.choices(string.digits, k=4))
    quiz_id = request.form.get('quiz_id')
    with get_db() as conn:
        while conn.execute("SELECT id FROM battles WHERE code=?", (code,)).fetchone(): code = ''.join(random.choices(string.digits, k=4))
        conn.execute("INSERT INTO battles (code, player1_id, quiz_id) VALUES (?,?,?)", (code, current_user.id, quiz_id))
    return redirect(url_for('battle_lobby', code=code))

@app.route('/battle/join', methods=['POST'])
@login_required
def join_battle():
    code = request.form.get('code', '').strip()
    with get_db() as conn:
        battle = conn.execute("SELECT * FROM battles WHERE code=? AND status='waiting'", (code,)).fetchone()
        if not battle: flash('Salon introuvable ou commencé.', 'error'); return redirect(url_for('battle_menu'))
        if battle['player1_id'] == current_user.id: flash('Vous ne pouvez pas vous affronter !', 'error'); return redirect(url_for('battle_menu'))
        conn.execute("UPDATE battles SET player2_id=?, status='ready' WHERE id=?", (current_user.id, battle['id']))
    return redirect(url_for('battle_lobby', code=code))

@app.route('/battle/lobby/<code>')
@login_required
def battle_lobby(code):
    with get_db() as conn:
        battle = conn.execute("SELECT b.*, q.title FROM battles b JOIN quizzes q ON b.quiz_id=q.id WHERE b.code=?", (code,)).fetchone()
        if not battle: return redirect(url_for('battle_menu'))
        p1 = conn.execute("SELECT username, avatar FROM users WHERE id=?", (battle['player1_id'],)).fetchone()
        p2 = conn.execute("SELECT username, avatar FROM users WHERE id=?", (battle['player2_id'],)).fetchone() if battle['player2_id'] else None
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center" style="min-height: 80vh; align-items: center;"><div class="col-md-6 text-center">
    <div class="card-neo" style="padding: 40px; border-color: var(--neon-pink);">
        <h3 style="font-family: Orbitron; color: var(--neon-pink); margin-bottom: 20px;">SALLE D'ATTENTE</h3>
        <div style="font-family: Orbitron; font-size: 3rem; letter-spacing: 10px; color: var(--text-primary); margin-bottom: 30px; background: var(--bg-card2); display: inline-block; padding: 10px 20px; border-radius: 10px;">{{ code }}</div>
        <p style="color: var(--text-muted); margin-bottom: 20px;">Quiz : {{ battle.title }}</p>
        <div style="display: flex; justify-content: center; gap: 30px; margin-bottom: 30px;">
            <div style="background: var(--bg-card2); padding: 15px; border-radius: 10px; width: 120px;"><div style="font-size: 2rem;">{{ p1.avatar }}</div><div style="font-weight: 700; font-size: 0.9rem;">{{ p1.username }}</div><div style="font-size: 0.7rem; color: var(--neon-green);">PRÊT</div></div>
            <div style="font-family: Orbitron; font-size: 2rem; color: var(--text-muted); align-self: center;">VS</div>
            <div style="background: var(--bg-card2); padding: 15px; border-radius: 10px; width: 120px;">
                {% if p2 %}<div style="font-size: 2rem;">{{ p2.avatar }}</div><div style="font-weight: 700; font-size: 0.9rem;">{{ p2.username }}</div><div style="font-size: 0.7rem; color: var(--neon-green);">PRÊT</div>
                {% else %}<div style="font-size: 2rem; color: var(--text-muted);">❓</div><div style="font-weight: 700; font-size: 0.9rem; color: var(--text-muted);">Attente...</div>{% endif %}
            </div>
        </div>
        <p style="color: var(--text-muted); font-size: 0.85rem;" id="statusText">{% if p2 %}Lancement...{% else %}En attente du joueur...{% endif %}</p>
    </div>
</div></div>
{% endblock %}
{% block scripts %}
<script>function checkStatus() { fetch('/api/battle/status/{{ code }}').then(r => r.json()).then(data => { if (data.status === 'ready') window.location.href = '/battle/play/' + data.battle_id; }); } setInterval(checkStatus, 2000);</script>
{% endblock %}
''', battle=battle, p1=p1, p2=p2)

@app.route('/api/battle/status/<code>')
def api_battle_status(code):
    with get_db() as conn:
        battle = conn.execute("SELECT id, status FROM battles WHERE code=?", (code,)).fetchone()
        if battle: return jsonify({"status": battle['status'], "battle_id": battle['id']})
    return jsonify({"status": "waiting"})

@app.route('/battle/play/<int:battle_id>', methods=['GET', 'POST'])
@login_required
def battle_play(battle_id):
    with get_db() as conn:
        battle = conn.execute("SELECT * FROM battles WHERE id=?", (battle_id,)).fetchone()
        if not battle or battle['status'] != 'ready': return redirect(url_for('battle_menu'))
        if current_user.id not in [battle['player1_id'], battle['player2_id']]: return redirect(url_for('battle_menu'))
        if (current_user.id == battle['player1_id'] and battle['attempt1_id']) or (current_user.id == battle['player2_id'] and battle['attempt2_id']): return redirect(url_for('battle_result', battle_id=battle_id))
        quiz_id = battle['quiz_id']
        quiz = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        questions = conn.execute('''SELECT q.id, q.text, q.q_type, q.options, q.correct_answer, q.explanation, q.points, q.time_per_question FROM quiz_questions qq JOIN questions q ON qq.question_id = q.id WHERE qq.quiz_id = ?''', (quiz_id,)).fetchall()

    if request.method == 'GET':
        return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center"><div class="col-lg-8">
    <div class="text-center mb-4"><span style="font-family: Orbitron; color: var(--neon-pink); letter-spacing: 2px;">⚔️ BATTLE ⚔️</span><h3 style="font-family: Orbitron; margin: 0;">{{ quiz.title }}</h3></div>
    <div style="display: flex; justify-content: center; margin-bottom: 16px;"><div class="timer-box" id="timer">00:30</div><div class="timer-progress" style="width: 120px; margin: 6px auto 0; background: rgba(0,245,255,0.1); border-radius: 4px; height: 6px; overflow: hidden;"><div class="timer-progress-fill" id="timerProgressFill" style="width: 100%; height: 100%; background: var(--neon-pink); border-radius: 4px; transition: width 1s linear;"></div></div></div>
    <div class="xp-progress-container mb-3"><div class="xp-progress-fill" id="progress-fill" style="width: 0%;"></div></div>
    <form method="POST" id="quizForm" novalidate><input type="hidden" name="time_taken" id="time_taken" value="0">
        {% for q in questions %}
        <div class="question-card" id="question_{{ loop.index0 }}" style="{% if not loop.first %}display: none;{% endif %}">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <div class="question-number">QUESTION {{ loop.index }} / {{ questions|length }}</div>
                <span style="color: var(--neon-orange); font-size: 0.75rem; font-family: Orbitron;">⏱ {{ q.time_per_question or 30 }}s</span>
            </div>
            <div class="question-text">{{ q.text }}</div>
            {% if q.q_type == 'mcq' %}{% set opts = q.options|from_json %}{% for opt in opts %}<div><input type="radio" name="ans_{{ q.id }}" id="bopt_{{ q.id }}_{{ loop.index }}" value="{{ opt }}" style="display:none;"><label for="bopt_{{ q.id }}_{{ loop.index }}" class="option-label">{{ opt }}</label></div>{% endfor %}
            {% elif q.q_type == 'code' %}<textarea class="form-control code-editor" name="ans_{{ q.id }}" rows="4" placeholder="Code..." style="display: none;"></textarea>
            {% else %}<textarea class="form-control" name="ans_{{ q.id }}" rows="3" placeholder="Réponse..."></textarea>{% endif %}
        </div>
        {% endfor %}
        <div style="display: flex; justify-content: space-between; margin: 20px 0 40px;">
            <button type="button" class="btn-neon btn" id="prevBtn" style="display: none;" onclick="prevQuestion()">← Précédent</button>
            <button type="button" class="btn-neon btn" id="nextBtn" onclick="nextQuestion()">Suivant →</button>
            <button type="submit" class="btn w-100" id="submitBtn" style="display: none; padding: 16px; font-size: 1rem; background: linear-gradient(135deg, #bf00ff, #ff0080); border: none; color: white; font-weight: 700; letter-spacing: 1px; border-radius: 6px;">VALIDER</button>
        </div>
    </form>
</div></div>
{% endblock %}
{% block scripts %}
<script>
const questionTimes = [{% for q in questions %}{{ q.time_per_question or 30 }}{% if not loop.last %},{% endif %}{% endfor %}];
const totalQuestions = {{ questions|length }};
let currentQ = 0, timeLeft = questionTimes[0], startTime = Date.now(), timerInterval = null;
const timerEl = document.getElementById('timer'), timerProgressFill = document.getElementById('timerProgressFill'), progressFill = document.getElementById('progress-fill');
const prevBtn = document.getElementById('prevBtn'), nextBtn = document.getElementById('nextBtn'), submitBtn = document.getElementById('submitBtn');
function startTimer() { clearInterval(timerInterval); timeLeft = questionTimes[currentQ]; updateTimerDisplay(); timerInterval = setInterval(() => { timeLeft--; updateTimerDisplay(); if (timeLeft <= 0) { clearInterval(timerInterval); if (currentQ < totalQuestions - 1) nextQuestion(); else submitQuiz(); } }, 1000); }
function updateTimerDisplay() { const m = String(Math.floor(timeLeft/60)).padStart(2,'0'), s = String(timeLeft%60).padStart(2,'0'); timerEl.textContent = m+':'+s; const pct = (timeLeft / questionTimes[currentQ]) * 100; timerProgressFill.style.width = pct + '%'; if(timeLeft <= 10){ timerEl.classList.add('danger'); timerProgressFill.style.background='var(--neon-pink)'; } else { timerEl.classList.remove('danger'); timerProgressFill.style.background='var(--neon-pink)'; } }
function showQuestion(i) { document.querySelectorAll('.question-card').forEach((c,idx) => { c.style.display = idx === i ? 'block' : 'none'; if(idx===i) c.querySelectorAll('.code-editor').forEach(t => { if(t.cmInstance) setTimeout(()=>t.cmInstance.refresh(),10); }); }); progressFill.style.width = ((i+1)/totalQuestions*100)+'%'; prevBtn.style.display = i > 0 ? 'inline-block' : 'none'; nextBtn.style.display = i < totalQuestions - 1 ? 'inline-block' : 'none'; submitBtn.style.display = i === totalQuestions - 1 ? 'inline-block' : 'none'; currentQ = i; startTimer(); }
function nextQuestion() { if(currentQ < totalQuestions - 1) showQuestion(currentQ + 1); }
function prevQuestion() { if(currentQ > 0) showQuestion(currentQ - 1); }
function submitQuiz() { clearInterval(timerInterval); document.getElementById('time_taken').value = Math.round((Date.now() - startTime) / 1000); document.querySelectorAll('.code-editor').forEach(t => { if(t.cmInstance) t.cmInstance.save(); }); document.getElementById('quizForm').submit(); }
document.getElementById('quizForm').addEventListener('submit', () => { clearInterval(timerInterval); document.getElementById('time_taken').value = Math.round((Date.now() - startTime) / 1000); document.querySelectorAll('.code-editor').forEach(t => { if(t.cmInstance) t.cmInstance.save(); }); });
document.querySelectorAll('input[type="radio"]').forEach(r => { r.addEventListener('change', function() { document.querySelectorAll('input[name="'+this.name+'"]').forEach(x => document.querySelector('label[for="'+x.id+'"]').classList.remove('option-selected')); document.querySelector('label[for="'+this.id+'"]').classList.add('option-selected'); }); });
document.querySelectorAll('.code-editor').forEach(t => { let cm = CodeMirror.fromTextArea(t, { lineNumbers: true, theme: "dracula", mode: "python" }); t.cmInstance = cm; });
startTimer();
</script>
{% endblock %}
''', quiz=quiz, questions=questions)

    elif request.method == 'POST':
        try:
            score = 0; total = len(questions); now = datetime.now().strftime('%Y-%m-%d %H:%M:%S'); time_taken = int(request.form.get('time_taken', 0))
            attempt_id = None; final_score = 0
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("INSERT INTO attempts (user_id, quiz_id, score, started_at, completed_at, time_taken) VALUES (?,?,?,?,?,?)", (current_user.id, quiz_id, 0, now, now, time_taken))
                attempt_id = cur.lastrowid
                for q in questions:
                    user_ans = request.form.get(f"ans_{q['id']}", '')
                    correct = json.loads(q['correct_answer'])
                    is_correct = 1 if (user_ans.strip().lower() == str(correct).strip().lower()) else 0
                    score += is_correct
                    cur.execute("INSERT INTO answers (attempt_id, question_id, user_answer, is_correct, time_spent) VALUES (?,?,?,?,?)", (attempt_id, q['id'], user_ans, is_correct, q['time_per_question'] or 30))
                final_score = (score / total) * 100
                conn.execute("UPDATE attempts SET score=? WHERE id=?", (final_score, attempt_id))
                if current_user.id == battle['player1_id']: conn.execute("UPDATE battles SET attempt1_id=? WHERE id=?", (attempt_id, battle_id))
                else: conn.execute("UPDATE battles SET attempt2_id=? WHERE id=?", (attempt_id, battle_id))
                conn.execute("INSERT OR IGNORE INTO user_badges VALUES (?,7,?)", (current_user.id, now))
            log_audit(current_user.id, f'Battle play bid={battle_id} score={final_score}%')
            return redirect(url_for('battle_result', battle_id=battle_id))
        except Exception as e:
            logger.error(f"Battle play error: {e}")
            flash(f'Erreur: {e}', 'error')
            return redirect(url_for('battle_play', battle_id=battle_id))

@app.route('/battle/result/<int:battle_id>')
@login_required
def battle_result(battle_id):
    with get_db() as conn:
        battle = conn.execute("SELECT * FROM battles WHERE id=?", (battle_id,)).fetchone()
        if not battle: return redirect(url_for('battle_menu'))
        p1 = conn.execute("SELECT id, username, avatar FROM users WHERE id=?", (battle['player1_id'],)).fetchone()
        p2 = conn.execute("SELECT id, username, avatar FROM users WHERE id=?", (battle['player2_id'],)).fetchone()
        a1 = conn.execute("SELECT score FROM attempts WHERE id=?", (battle['attempt1_id'],)).fetchone() if battle['attempt1_id'] else None
        a2 = conn.execute("SELECT score FROM attempts WHERE id=?", (battle['attempt2_id'],)).fetchone() if battle['attempt2_id'] else None
        if not a1 or not a2:
            return render_template_string('''{% extends "base.html" %}{% block content %}<div class="row justify-content-center" style="min-height: 80vh; align-items: center;"><div class="col-md-6 text-center"><div class="card-neo" style="padding: 40px; border-color: var(--neon-orange);"><div style="font-size: 4rem; margin-bottom: 20px;">⏳</div><h3 style="font-family: Orbitron; color: var(--neon-orange);">EN ATTENTE</h3><p style="color: var(--text-muted);">Score enregistré. Attente de l'adversaire.</p><script>setTimeout(() => { window.location.reload(); }, 3000);</script></div></div></div>{% endblock %}''')
        s1 = a1['score'] if a1 else 0; s2 = a2['score'] if a2 else 0; is_draw = s1 == s2; p1_win = s1 > s2
        conn.execute("UPDATE battles SET status='finished' WHERE id=?", (battle_id,))
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center" style="min-height: 80vh; align-items: center;"><div class="col-lg-9">
    <div class="text-center mb-5"><h2 style="font-family: Orbitron; color: var(--neon-pink);">RÉSULTAT DU DUEL</h2><div style="font-size: 1.5rem; color: var(--text-primary); margin-top: 10px;">{% if is_draw %}ÉGALITÉ ! 🤝{% elif (current_user.id == p1.id and p1_win) or (current_user.id == p2.id and not p1_win) %}VICTOIRE ! 🏆{% else %}DÉFAITE... 📚{% endif %}</div></div>
    <div style="display: flex; justify-content: center; align-items: center; gap: 30px; flex-wrap: wrap;">
        <div class="battle-player-card {% if p1_win and not is_draw %}battle-winner{% elif not p1_win and not is_draw %}battle-loser{% endif %}"><div style="font-size: 4rem;">{{ p1.avatar }}</div><div style="font-weight: 700; font-size: 1.2rem; margin: 10px 0;">{{ p1.username }}</div><div style="font-family: Orbitron; font-size: 3rem; color: var(--neon-cyan);">{{ "%.0f"|format(s1) }}%</div>{% if p1_win and not is_draw %}<div style="color: var(--neon-green); font-weight: 700; margin-top: 10px;">GAGNANT</div>{% endif %}</div>
        <div class="vs-text">VS</div>
        <div class="battle-player-card {% if not p1_win and not is_draw %}battle-winner{% elif p1_win and not is_draw %}battle-loser{% endif %}"><div style="font-size: 4rem;">{{ p2.avatar }}</div><div style="font-weight: 700; font-size: 1.2rem; margin: 10px 0;">{{ p2.username }}</div><div style="font-family: Orbitron; font-size: 3rem; color: var(--neon-purple);">{{ "%.0f"|format(s2) }}%</div>{% if not p1_win and not is_draw %}<div style="color: var(--neon-green); font-weight: 700; margin-top: 10px;">GAGNANT</div>{% endif %}</div>
    </div>
    <div class="text-center mt-5"><a href="{{ url_for('battle_menu') }}" class="btn btn-neon btn-neon-purple">Rejouer ⚔️</a><a href="{{ url_for('student_dashboard') }}" class="btn-neon btn ms-2">Dashboard ←</a></div>
</div></div>
{% endblock %}
''', p1=p1, p2=p2, s1=s1, s2=s2, is_draw=is_draw, p1_win=p1_win)

# ==========================================
# MECANIQUES COLLABORATIVES (TEAM BATTLE)
# ==========================================
@app.route('/battle/team', methods=['GET', 'POST'])
@login_required
def team_battle_menu():
    if current_user.role != 'student': return redirect(url_for('login'))
    if request.method == 'POST':
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        quiz_id = request.form.get('quiz_id')
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO team_battles (code, quiz_id, creator_id) VALUES (?,?,?)", (code, quiz_id, current_user.id))
            team_id = cur.lastrowid
            conn.execute("INSERT INTO team_battle_members VALUES (?,?,NULL)", (team_id, current_user.id))
        return redirect(url_for('team_lobby', code=code))
    
    with get_db() as conn:
        quizzes = conn.execute("SELECT id, title FROM quizzes WHERE is_daily_challenge=0").fetchall()
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center"><div class="col-md-6">
    <h2 style="font-family: Orbitron; color: var(--neon-green); margin-bottom: 20px;">🤝 DÉFI D'ÉQUIPE</h2>
    <p style="color: var(--text-muted); margin-bottom: 30px;">Rejoignez vos forces. Le score de l'équipe est la moyenne de tous les membres.</p>
    <div class="card-neo mb-4" style="padding: 24px;">
        <form method="POST">
            <select name="quiz_id" class="form-select mb-3">{% for q in quizzes %}<option value="{{ q.id }}">{{ q.title }}</option>{% endfor %}</select>
            <button type="submit" class="btn w-100 btn-neon-green" style="padding: 13px;">Créer une salle d'équipe</button>
        </form>
    </div>
    <div class="card-neo" style="padding: 24px;">
        <form method="POST" action="{{ url_for('join_team_battle') }}">
            <input type="text" name="code" class="form-control mb-3" placeholder="Code équipe (ex: A1B2C3)" style="text-align:center; letter-spacing:5px; font-family:Orbitron;">
            <button type="submit" class="btn btn-neon w-100">Rejoindre une équipe</button>
        </form>
    </div>
</div></div>
{% endblock %}
''', quizzes=quizzes)

@app.route('/battle/team/join', methods=['POST'])
@login_required
def join_team_battle():
    code = request.form.get('code', '').strip().upper()
    with get_db() as conn:
        team = conn.execute("SELECT * FROM team_battles WHERE code=? AND status='waiting'", (code,)).fetchone()
        if not team: flash('Équipe introuvable.', 'error'); return redirect(url_for('team_battle_menu'))
        count = conn.execute("SELECT COUNT(*) FROM team_battle_members WHERE team_battle_id=?", (team['id'],)).fetchone()[0]
        if count >= team['max_members']: flash('Équipe pleine.', 'error'); return redirect(url_for('team_battle_menu'))
        conn.execute("INSERT INTO team_battle_members VALUES (?,?,NULL)", (team['id'], current_user.id))
    return redirect(url_for('team_lobby', code=code))

@app.route('/battle/team/lobby/<code>')
@login_required
def team_lobby(code):
    with get_db() as conn:
        team = conn.execute("SELECT tb.*, q.title FROM team_battles tb JOIN quizzes q ON tb.quiz_id=q.id WHERE tb.code=?", (code,)).fetchone()
        if not team: return redirect(url_for('team_battle_menu'))
        members = conn.execute('''SELECT u.username, u.avatar FROM team_battle_members tbm JOIN users u ON tbm.user_id=u.id WHERE tbm.team_battle_id=?''', (team['id'],)).fetchall()
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="text-center" style="padding-top: 10vh;">
    <div class="card-neo" style="padding: 40px; max-width: 500px; margin: 0 auto; border-color: var(--neon-green);">
        <h3 style="color: var(--neon-green); font-family: Orbitron;">SALLE D'ÉQUIPE</h3>
        <div style="font-family: Orbitron; font-size: 2rem; margin: 20px 0; letter-spacing: 5px;">{{ code }}</div>
        <p style="color: var(--text-muted);">{{ team.title }}</p>
        <div style="display: flex; justify-content: center; gap: 15px; margin: 30px 0; flex-wrap: wrap;">
            {% for m in members %}<div style="background: var(--bg-card2); padding: 10px 15px; border-radius: 10px;"><div style="font-size: 2rem;">{{ m.avatar }}</div><div style="font-size: 0.8rem;">{{ m.username }}</div></div>{% endfor %}
        </div>
        <a href="{{ url_for('take_quiz', quiz_id=team.quiz_id) }}" class="btn btn-neon-green w-100" style="padding: 15px;">Lancer la mission commune →</a>
        <p style="color: var(--text-muted); font-size: 0.75rem; margin-top: 15px;">Partagez le code {{ code }} à vos coéquipiers</p>
    </div>
</div>
{% endblock %}
''', team=team, members=members)

@app.route('/leaderboard')
@login_required
def leaderboard():
    with get_db() as conn:
        top_users = conn.execute('''SELECT u.username, u.avatar, u.xp, u.streak, COUNT(a.id) as total_quizzes, COALESCE(AVG(a.score), 0) as avg_score FROM users u LEFT JOIN attempts a ON u.id = a.user_id WHERE u.role = 'student' GROUP BY u.id ORDER BY u.xp DESC LIMIT 20''').fetchall()
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="row justify-content-center"><div class="col-lg-7">
    <div class="text-center mb-5"><h2 style="font-family: Orbitron;">🏆 CLASSEMENT</h2></div>
    {% for u in top_users %}<div class="leaderboard-item rank-{{ loop.index }}"><div class="rank-number">{% if loop.index == 1 %}🥇{% elif loop.index == 2 %}🥈{% elif loop.index == 3 %}🥉{% else %}#{{ loop.index }}{% endif %}</div><span style="font-size: 1.8rem; margin: 0 12px;">{{ u.avatar }}</span><div style="flex: 1;"><div style="font-weight: 700;">{{ u.username }}</div><div style="font-size: 0.75rem; color: var(--text-muted);">{{ u.total_quizzes }} quizzes • {{ "%.0f"|format(u.avg_score) }}%</div></div><div style="text-align: right;"><div style="font-family: Orbitron; color: var(--neon-cyan);">{{ u.xp }} XP</div>{% if u.streak > 0 %}<div style="font-size: 0.75rem; color: var(--neon-orange);">🔥 {{ u.streak }}</div>{% endif %}</div></div>{% endfor %}
</div></div>
{% endblock %}
''', top_users=top_users)

# ==========================================
# TEACHER ROUTES
# ==========================================
@app.route('/teacher/dashboard')
@login_required
def teacher_dashboard():
    if current_user.role != 'teacher': return redirect(url_for('login'))
    with get_db() as conn:
        total_students = conn.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0]
        total_attempts = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        avg_score = conn.execute("SELECT AVG(score) FROM attempts").fetchone()[0] or 0
        total_questions = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        score_dist = conn.execute("SELECT CASE WHEN score >= 80 THEN 'Excellent' WHEN score >= 50 THEN 'Moyen' ELSE 'Faible' END as range, COUNT(*) as count FROM attempts GROUP BY range").fetchall()
        student_stats = conn.execute('''SELECT u.username, u.avatar, u.xp, u.current_level, COUNT(a.id) as attempts, COALESCE(AVG(a.score), 0) as avg_score FROM users u LEFT JOIN attempts a ON u.id = a.user_id WHERE u.role = 'student' GROUP BY u.id ORDER BY u.xp DESC''').fetchall()
        recent_battles = conn.execute('''SELECT b.code, u1.username as p1, u2.username as p2, a1.score as s1, a2.score as s2 FROM battles b JOIN users u1 ON b.player1_id=u1.id JOIN users u2 ON b.player2_id=u2.id LEFT JOIN attempts a1 ON b.attempt1_id=a1.id LEFT JOIN attempts a2 ON b.attempt2_id=a2.id WHERE b.status='finished' ORDER BY b.created_at DESC LIMIT 5''').fetchall()
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-4"><div><h2 style="font-family: Orbitron; margin: 0;">ANALYTICS</h2></div><a href="{{ url_for('export_csv') }}" class="btn-neon btn">↓ Export CSV</a></div>
<div class="row g-3 mb-5"><div class="col-6 col-md-3"><div class="stat-card stat-cyan"><div class="stat-number" style="color: var(--neon-cyan);">{{ total_students }}</div><div class="stat-label">Apprenants</div></div></div><div class="col-6 col-md-3"><div class="stat-card stat-green"><div class="stat-number" style="color: var(--neon-green);">{{ total_attempts }}</div><div class="stat-label">Tentatives</div></div></div><div class="col-6 col-md-3"><div class="stat-card stat-purple"><div class="stat-number" style="color: var(--neon-purple);">{{ "%.1f"|format(avg_score) }}%</div><div class="stat-label">Moy.</div></div></div><div class="col-6 col-md-3"><div class="stat-card stat-orange"><div class="stat-number" style="color: var(--neon-orange);">{{ total_questions }}</div><div class="stat-label">Questions</div></div></div></div>
<div class="row g-4 mb-4">
    <div class="col-md-5"><div class="card-neo" style="padding: 24px; height: 100%;"><p class="section-title">Distribution</p><canvas id="scoreChart"></canvas></div></div>
    <div class="col-md-7"><div class="card-neo" style="padding: 24px;"><p class="section-title">Apprenants</p><table class="table table-neo"><thead><tr><th>Apprenant</th><th>Niveau</th><th>XP</th><th>Moy.</th></tr></thead><tbody>{% for s in student_stats %}<tr><td>{{ s.avatar }} {{ s.username }}</td><td><span class="diff-{{ s.current_level }}">{{ s.current_level|upper }}</span></td><td style="color: var(--neon-cyan); font-family: Orbitron;">{{ s.xp }}</td><td style="color: {{ 'var(--neon-green)' if s.avg_score >= 80 else 'var(--neon-orange)' }};">{{ "%.0f"|format(s.avg_score) }}%</td></tr>{% endfor %}</tbody></table></div></div>
</div>
<div class="card-neo" style="padding: 24px;"><p class="section-title">⚔️ Dernières Battles</p>{% for b in recent_battles %}<div style="display: flex; justify-content: space-between; padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.03);"><span>{{ b.p1 }} <span style="color: var(--neon-pink);">VS</span> {{ b.p2 }}</span><span style="font-family: Orbitron; color: var(--neon-cyan);">{{ "%.0f"|format(b.s1) }}% - {{ "%.0f"|format(b.s2) }}%</span></div>{% endfor %}</div>
{% endblock %}
{% block scripts %}
<script>const ctx = document.getElementById('scoreChart').getContext('2d'); new Chart(ctx, { type: 'doughnut', data: { labels: [{% for s in score_dist %}'{{ s.range }}'{% if not loop.last %},{% endif %}{% endfor %}], datasets: [{ data: [{% for s in score_dist %}{{ s.count }}{% if not loop.last %},{% endif %}{% endfor %}], backgroundColor: ['rgba(0,255,136,0.7)', 'rgba(255,107,53,0.7)', 'rgba(255,0,128,0.7)'], borderWidth: 0 }] }, options: { plugins: { legend: { labels: { color: '#6a8aad' } } }, cutout: '65%' } });</script>
{% endblock %}
''', total_students=total_students, total_attempts=total_attempts, avg_score=avg_score, total_questions=total_questions, score_dist=score_dist, student_stats=student_stats, recent_battles=recent_battles)

# ==========================================
# TEACHER : GESTION DES SOUS-CATÉGORIES
# ==========================================
@app.route('/teacher/subcategories', methods=['GET', 'POST'])
@login_required
def manage_subcategories():
    if current_user.role != 'teacher': return redirect(url_for('login'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        category_id = request.form.get('category_id')
        if name and category_id:
            with get_db() as conn:
                conn.execute("INSERT INTO subcategories (category_id, name) VALUES (?,?)", (int(category_id), name))
            flash('Sous-catégorie ajoutée !', 'success')
            return redirect(url_for('manage_subcategories'))
        else:
            flash('Nom et catégorie requis.', 'error')

    with get_db() as conn:
        categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
        subcategories = conn.execute('''SELECT sc.*, c.name as cat_name, c.icon as cat_icon FROM subcategories sc JOIN categories c ON sc.category_id = c.id ORDER BY c.name, sc.name''').fetchall()
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<h2 style="font-family: Orbitron; margin-bottom: 20px;">SOUS-CATÉGORIES</h2>
<div class="row g-4">
    <div class="col-md-4">
        <div class="card-neo" style="padding: 24px; position: sticky; top: 80px;">
            <p class="section-title">Ajouter</p>
            <form method="POST">
                <div class="mb-3"><label class="mb-1">Catégorie</label><select name="category_id" class="form-select" required>{% for c in categories %}<option value="{{ c.id }}">{{ c.icon }} {{ c.name }}</option>{% endfor %}</select></div>
                <div class="mb-3"><label class="mb-1">Nom de la sous-catégorie</label><input type="text" name="name" class="form-control" required placeholder="Ex: Notion d'algorithme"></div>
                <button type="submit" class="btn btn-solid-cyan w-100">+ Enregistrer</button>
            </form>
        </div>
    </div>
    <div class="col-md-8">
        {% for sc in subcategories %}
        <div class="card-neo mb-3" style="padding: 18px;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <div style="font-size: 0.7rem; color: var(--text-muted);">#{{ sc.id }} • {{ sc.cat_icon }} {{ sc.cat_name }}</div>
                    <div style="font-weight: 600; font-size: 1rem;">📁 {{ sc.name }}</div>
                </div>
                <div style="display: flex; align-items: center; gap: 10px;">
                    <form method="POST" action="{{ url_for('delete_subcategory', sc_id=sc.id) }}" style="margin:0;" onsubmit="return confirm('Supprimer cette sous-catégorie ? Les questions associées perdront leur sous-catégorie.');">
                        <button type="submit" class="btn-neon btn btn-sm" style="border-color: var(--neon-pink); color: var(--neon-pink);">🗑️</button>
                    </form>
                </div>
            </div>
        </div>
        {% endfor %}
        {% if not subcategories %}
        <div class="card-neo" style="padding: 40px; text-align: center; color: var(--text-muted);">Aucune sous-catégorie.</div>
        {% endif %}
    </div>
</div>
{% endblock %}
''', categories=categories, subcategories=subcategories)

@app.route('/teacher/subcategories/delete/<int:sc_id>', methods=['POST'])
@login_required
def delete_subcategory(sc_id):
    if current_user.role != 'teacher': return redirect(url_for('login'))
    with get_db() as conn:
        conn.execute("UPDATE questions SET subcategory_id = NULL WHERE subcategory_id=?", (sc_id,))
        quiz_ids = [row['id'] for row in conn.execute("SELECT id FROM quizzes WHERE subcategory_id=?", (sc_id,)).fetchall()]
        for qid in quiz_ids:
            conn.execute("DELETE FROM quiz_questions WHERE quiz_id=?", (qid,))
            conn.execute("DELETE FROM quizzes WHERE id=?", (qid,))
        conn.execute("DELETE FROM subcategories WHERE id=?", (sc_id,))
    flash('Sous-catégorie supprimée.', 'success')
    return redirect(url_for('manage_subcategories'))

# ==========================================
# TEACHER : GESTION DES QUESTIONS
# ==========================================
@app.route('/teacher/questions', methods=['GET', 'POST'])
@login_required
def manage_questions():
    if current_user.role != 'teacher': return redirect(url_for('login'))
    if request.method == 'POST':
        try:
            q_type = request.form.get('q_type')
            options = json.dumps(request.form.getlist('options')) if q_type == 'mcq' else None
            subcategory_id = request.form.get('subcategory_id')
            subcategory_id = int(subcategory_id) if subcategory_id else None
            category_id = request.form.get('category_id')
            if subcategory_id:
                with get_db() as conn:
                    sc = conn.execute("SELECT category_id FROM subcategories WHERE id=?", (subcategory_id,)).fetchone()
                    if sc: category_id = sc['category_id']
            category_id = int(category_id) if category_id else None

            with get_db() as conn:
                conn.execute('''INSERT INTO questions (category_id, subcategory_id, text, q_type, options, correct_answer, difficulty, explanation, points, time_per_question) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                             (category_id, subcategory_id, request.form.get('text'), q_type, options, json.dumps(request.form.get('correct_answer')), request.form.get('difficulty'), request.form.get('explanation'), request.form.get('points', 10), request.form.get('time_per_question', 30)))
            refresh_all_quizzes()
            flash('Question ajoutée !', 'success')
            return redirect(url_for('manage_questions'))
        except Exception as e:
            flash(f'Erreur: {e}', 'error')

    with get_db() as conn:
        questions = conn.execute('''SELECT q.*, c.name as cat_name, sc.name as subcat_name FROM questions q LEFT JOIN categories c ON q.category_id = c.id LEFT JOIN subcategories sc ON q.subcategory_id = sc.id ORDER BY q.id DESC''').fetchall()
        categories = conn.execute("SELECT * FROM categories").fetchall()
        subcategories = conn.execute("SELECT * FROM subcategories ORDER BY category_id, name").fetchall()
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<h2 style="font-family: Orbitron; margin-bottom: 20px;">QUESTIONS</h2>
<div class="row g-4">
    <div class="col-md-4">
        <div class="card-neo" style="padding: 24px; position: sticky; top: 80px;">
            <p class="section-title">Ajouter</p>
            <form method="POST">
                <div class="mb-3"><label class="mb-1">Catégorie</label><select name="category_id" id="catSelect" class="form-select" required onchange="filterSubcategories()"><option value="">-- Sélectionner --</option>{% for c in categories %}<option value="{{ c.id }}">{{ c.icon }} {{ c.name }}</option>{% endfor %}</select></div>
                <div class="mb-3"><label class="mb-1">Sous-catégorie</label><select name="subcategory_id" id="subcatSelect" class="form-select"><option value="">-- Aucune --</option>{% for sc in subcategories %}<option value="{{ sc.id }}" data-cat="{{ sc.category_id }}">{{ sc.name }}</option>{% endfor %}</select></div>
                <div class="mb-3"><label class="mb-1">Type</label><select name="q_type" id="q_type" class="form-select" required onchange="toggleOptions()"><option value="mcq">QCM</option><option value="fill_blank">Trou</option><option value="match">Association</option><option value="code">Code</option></select></div>
                <div class="mb-3"><label class="mb-1">Difficulté</label><select name="difficulty" class="form-select" required><option value="easy">Facile</option><option value="medium">Moyen</option><option value="hard">Difficile</option></select></div>
                <div class="mb-3"><label class="mb-1">Points</label><input type="number" name="points" class="form-control" value="10" min="1" max="100"></div>
                <div class="mb-3"><label class="mb-1">Temps / question (s)</label><input type="number" name="time_per_question" class="form-control" value="30" min="5" max="300"></div>
                <div class="mb-3"><label class="mb-1">Question</label><textarea name="text" class="form-control" rows="3" required></textarea></div>
                <div id="options-container" class="mb-3"><label class="mb-1">Options QCM</label><input type="text" name="options" class="form-control mb-1" placeholder="Opt 1"><input type="text" name="options" class="form-control mb-1" placeholder="Opt 2"><input type="text" name="options" class="form-control mb-1" placeholder="Opt 3"><input type="text" name="options" class="form-control" placeholder="Opt 4"></div>
                <div class="mb-3"><label class="mb-1">Réponse correcte</label><input type="text" name="correct_answer" class="form-control" required></div>
                <div class="mb-3"><label class="mb-1">Explication</label><textarea name="explanation" class="form-control" rows="2"></textarea></div>
                <button type="submit" class="btn btn-solid-cyan w-100">+ Enregistrer</button>
            </form>
        </div>
    </div>
    <div class="col-md-8">
        {% for q in questions %}
        <div class="card-neo mb-3" style="padding: 18px;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div style="flex: 1;">
                    <div style="font-size: 0.7rem; color: var(--text-muted);">#{{ q.id }} • {{ q.cat_name }}{% if q.subcat_name %} / {{ q.subcat_name }}{% endif %} • {{ q.q_type|upper }} • ⏱ {{ q.time_per_question or 30 }}s</div>
                    <div style="font-weight: 600; color: var(--text-primary);">{{ q.text[:80] }}{% if q.text|length > 80 %}...{% endif %}</div>
                </div>
                <div style="display: flex; align-items: center; gap: 10px;">
                    <span class="diff-{{ q.difficulty }}">{{ q.difficulty|upper }}</span>
                    <span style="font-family: Orbitron; font-size: 0.7rem; color: var(--neon-cyan);">{{ q.points }} pts</span>
                    <a href="{{ url_for('edit_question', q_id=q.id) }}" class="btn-neon btn btn-sm">✏️</a>
                    <form method="POST" action="{{ url_for('delete_question', q_id=q.id) }}" style="margin:0;" onsubmit="return confirm('Supprimer ?');">
                        <button type="submit" class="btn-neon btn btn-sm" style="border-color: var(--neon-pink); color: var(--neon-pink);">🗑️</button>
                    </form>
                </div>
            </div>
        </div>
        {% endfor %}
    </div>
</div>
{% endblock %}
{% block scripts %}
<script>
function toggleOptions() { document.getElementById('options-container').style.display = document.getElementById('q_type').value === 'mcq' ? 'block' : 'none'; }
function filterSubcategories() {
    const catId = document.getElementById('catSelect').value;
    const sel = document.getElementById('subcatSelect');
    sel.value = '';
    Array.from(sel.options).forEach(opt => {
        opt.style.display = (!opt.value || opt.dataset.cat === catId) ? '' : 'none';
    });
}
</script>
{% endblock %}
''', questions=questions, categories=categories, subcategories=subcategories)

@app.route('/teacher/questions/delete/<int:q_id>', methods=['POST'])
@login_required
def delete_question(q_id):
    if current_user.role != 'teacher': return redirect(url_for('login'))
    with get_db() as conn:
        conn.execute("DELETE FROM quiz_questions WHERE question_id=?", (q_id,))
        conn.execute("DELETE FROM questions WHERE id=?", (q_id,))
    refresh_all_quizzes()
    flash('Question supprimée.', 'success')
    return redirect(url_for('manage_questions'))

@app.route('/teacher/questions/edit/<int:q_id>', methods=['GET', 'POST'])
@login_required
def edit_question(q_id):
    if current_user.role != 'teacher': return redirect(url_for('login'))
    if request.method == 'POST':
        try:
            q_type = request.form.get('q_type')
            options = json.dumps(request.form.getlist('options')) if q_type == 'mcq' else None
            subcategory_id = request.form.get('subcategory_id')
            subcategory_id = int(subcategory_id) if subcategory_id else None
            category_id = request.form.get('category_id')
            if subcategory_id:
                with get_db() as conn:
                    sc = conn.execute("SELECT category_id FROM subcategories WHERE id=?", (subcategory_id,)).fetchone()
                    if sc: category_id = sc['category_id']
            category_id = int(category_id) if category_id else None
            with get_db() as conn:
                conn.execute('''UPDATE questions SET category_id=?, subcategory_id=?, text=?, q_type=?, options=?, correct_answer=?, difficulty=?, explanation=?, points=?, time_per_question=? WHERE id=?''',
                    (category_id, subcategory_id, request.form.get('text'), q_type, options,
                     json.dumps(request.form.get('correct_answer')), request.form.get('difficulty'),
                     request.form.get('explanation'), request.form.get('points', 10), request.form.get('time_per_question', 30), q_id))
            refresh_all_quizzes()
            flash('Question mise à jour !', 'success')
            return redirect(url_for('manage_questions'))
        except Exception as e: flash(f'Erreur: {e}', 'error')

    with get_db() as conn:
        q = conn.execute("SELECT * FROM questions WHERE id=?", (q_id,)).fetchone()
        if not q: flash('Question introuvable.', 'error'); return redirect(url_for('manage_questions'))
        categories = conn.execute("SELECT * FROM categories").fetchall()
        subcategories = conn.execute("SELECT * FROM subcategories ORDER BY category_id, name").fetchall()
        opts = json.loads(q['options']) if q['options'] else []
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-4"><h2 style="font-family: Orbitron; margin: 0;">MODIFIER #{{ q.id }}</h2><a href="{{ url_for('manage_questions') }}" class="btn-neon btn">← Retour</a></div>
<div class="row justify-content-center"><div class="col-lg-7"><div class="card-neo" style="padding: 30px;">
    <form method="POST">
        <div class="mb-3"><label class="mb-1">Catégorie</label><select name="category_id" id="catSelect" class="form-select" required onchange="filterEditSubcategories()">{% for c in categories %}<option value="{{ c.id }}" {% if c.id == q.category_id %}selected{% endif %}>{{ c.icon }} {{ c.name }}</option>{% endfor %}</select></div>
        <div class="mb-3"><label class="mb-1">Sous-catégorie</label><select name="subcategory_id" id="subcatSelect" class="form-select"><option value="">-- Aucune --</option>{% for sc in subcategories %}<option value="{{ sc.id }}" data-cat="{{ sc.category_id }}" {% if sc.id == q.subcategory_id %}selected{% endif %}>{{ sc.name }}</option>{% endfor %}</select></div>
        <div class="mb-3"><label class="mb-1">Type</label><select name="q_type" id="q_type" class="form-select" required onchange="toggleEditOptions()"><option value="mcq" {% if q.q_type == 'mcq' %}selected{% endif %}>QCM</option><option value="fill_blank" {% if q.q_type == 'fill_blank' %}selected{% endif %}>Trou</option><option value="match" {% if q.q_type == 'match' %}selected{% endif %}>Association</option><option value="code" {% if q.q_type == 'code' %}selected{% endif %}>Code</option></select></div>
        <div class="mb-3"><label class="mb-1">Difficulté</label><select name="difficulty" class="form-select" required><option value="easy" {% if q.difficulty == 'easy' %}selected{% endif %}>Facile</option><option value="medium" {% if q.difficulty == 'medium' %}selected{% endif %}>Moyen</option><option value="hard" {% if q.difficulty == 'hard' %}selected{% endif %}>Difficile</option></select></div>
        <div class="mb-3"><label class="mb-1">Points</label><input type="number" name="points" class="form-control" value="{{ q.points }}" min="1" max="100"></div>
        <div class="mb-3"><label class="mb-1">Temps / question (s)</label><input type="number" name="time_per_question" class="form-control" value="{{ q.time_per_question or 30 }}" min="5" max="300"></div>
        <div class="mb-3"><label class="mb-1">Question</label><textarea name="text" class="form-control" rows="3" required>{{ q.text }}</textarea></div>
        <div id="edit-options-container" class="mb-3" style="{% if q.q_type != 'mcq' %}display: none;{% endif %}">
            <label class="mb-1">Options QCM</label>
            <input type="text" name="options" class="form-control mb-2" placeholder="Option 1" value="{{ opts[0] if opts|length > 0 else '' }}">
            <input type="text" name="options" class="form-control mb-2" placeholder="Option 2" value="{{ opts[1] if opts|length > 1 else '' }}">
            <input type="text" name="options" class="form-control mb-2" placeholder="Option 3" value="{{ opts[2] if opts|length > 2 else '' }}">
            <input type="text" name="options" class="form-control" placeholder="Option 4" value="{{ opts[3] if opts|length > 3 else '' }}">
        </div>
        <div class="mb-3"><label class="mb-1">Réponse correcte</label><input type="text" name="correct_answer" class="form-control" value="{{ q.correct_answer }}" required></div>
        <div class="mb-4"><label class="mb-1">Explication</label><textarea name="explanation" class="form-control" rows="2">{{ q.explanation or '' }}</textarea></div>
        <button type="submit" class="btn btn-solid-cyan w-100">💾 Sauvegarder</button>
    </form>
</div></div></div>
{% endblock %}
{% block scripts %}
<script>
function toggleEditOptions() { document.getElementById('edit-options-container').style.display = document.getElementById('q_type').value === 'mcq' ? 'block' : 'none'; }
function filterEditSubcategories() {
    const catId = document.getElementById('catSelect').value;
    const sel = document.getElementById('subcatSelect');
    sel.value = '';
    Array.from(sel.options).forEach(opt => {
        opt.style.display = (!opt.value || opt.dataset.cat === catId) ? '' : 'none';
    });
}
toggleEditOptions();
</script>
{% endblock %}
''', q=q, categories=categories, subcategories=subcategories, opts=opts)

# ==========================================
# TEACHER : GESTION DES QUIZZES (auto-générés)
# ==========================================
@app.route('/teacher/quizzes', methods=['GET', 'POST'])
@login_required
def manage_quizzes():
    if current_user.role != 'teacher': return redirect(url_for('login'))
    if request.method == 'POST':
        refresh_all_quizzes()
        flash('Quizzes rafraîchis automatiquement.', 'success')
        return redirect(url_for('manage_quizzes'))

    with get_db() as conn:
        quizzes = conn.execute('''
            SELECT q.*, c.name as cat_name, c.icon as cat_icon,
                   sc.name as subcat_name,
                   (SELECT COUNT(*) FROM quiz_questions WHERE quiz_id=q.id) as q_count
            FROM quizzes q
            LEFT JOIN categories c ON q.category_id = c.id
            LEFT JOIN subcategories sc ON q.subcategory_id = sc.id
            ORDER BY q.is_daily_challenge DESC, c.name, sc.name, q.difficulty
        ''').fetchall()
    return render_template_string('''
{% extends "base.html" %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-4">
    <div><h2 style="font-family: Orbitron; margin: 0;">QUIZZES AUTO-GÉNÉRÉS</h2>
    <p style="color: var(--text-muted); font-size: 0.85rem; margin: 0;">Générés automatiquement à partir des questions par sous-catégorie et difficulté</p></div>
    <div style="display: flex; gap: 10px;">
        <form method="POST"><button type="submit" class="btn-neon btn">🔄 Rafraîchir</button></form>
    </div>
</div>
<div class="row g-3">
    {% for q in quizzes %}
    <div class="col-md-6 col-lg-4">
        <div class="card-neo h-100" style="padding: 20px;">
            <div style="display: flex; justify-content: space-between; align-items: start; margin-bottom: 10px;">
                {% if q.is_daily_challenge %}
                <span style="background: linear-gradient(135deg, rgba(255,107,53,0.3), rgba(255,0,128,0.3)); border: 1px solid var(--neon-orange); border-radius: 8px; padding: 4px 10px; font-size: 0.7rem; color: var(--neon-orange); font-weight: 700;">⚡ DÉFI JOUR</span>
                {% else %}
                <span style="font-size: 1.3rem;">{{ q.cat_icon }}</span>
                {% endif %}
                <span class="diff-{{ q.difficulty }}">{{ q.difficulty|upper }}</span>
            </div>
            <h5 style="font-weight: 700; margin-bottom: 6px; font-size: 0.95rem;">{{ q.title }}</h5>
            <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 14px;">
                {% if q.subcat_name %}📁 {{ q.subcat_name }}{% endif %}
                {% if q.cat_name and not q.subcat_name %}{{ q.cat_name }}{% endif %}
            </div>
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <span style="font-family: Orbitron; font-size: 0.75rem; color: var(--neon-cyan);">{{ q.q_count }} question{{ 's' if q.q_count > 1 else '' }}</span>
                {% if not q.is_daily_challenge %}
                <form method="POST" action="{{ url_for('delete_quiz', quiz_id=q.id) }}" style="margin:0;" onsubmit="return confirm('Supprimer ce quiz auto-généré ? Il sera recréé au prochain rafraîchissement si des questions existent.');">
                    <button type="submit" class="btn-neon btn btn-sm" style="border-color: var(--neon-pink); color: var(--neon-pink); font-size: 0.7rem; padding: 3px 8px;">🗑️</button>
                </form>
                {% endif %}
            </div>
        </div>
    </div>
    {% endfor %}
    {% if not quizzes %}
    <div class="col-12"><div class="card-neo" style="padding: 60px; text-align: center;"><div style="font-size: 3rem; margin-bottom: 16px;">📭</div><p style="color: var(--text-muted);">Aucun quiz. Ajoutez des questions avec sous-catégorie et difficulté pour les voir apparaître.</p></div></div>
    {% endif %}
</div>
{% endblock %}
''', quizzes=quizzes)

@app.route('/teacher/quizzes/delete/<int:quiz_id>', methods=['POST'])
@login_required
def delete_quiz(quiz_id):
    if current_user.role != 'teacher': return redirect(url_for('login'))
    with get_db() as conn:
        quiz = conn.execute("SELECT is_daily_challenge FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        if quiz and quiz['is_daily_challenge']:
            flash('Le défi du jour ne peut pas être supprimé manuellement.', 'error')
            return redirect(url_for('manage_quizzes'))
        conn.execute("DELETE FROM quiz_questions WHERE quiz_id=?", (quiz_id,))
        conn.execute("DELETE FROM quizzes WHERE id=?", (quiz_id,))
    flash('Quiz supprimé.', 'success')
    return redirect(url_for('manage_quizzes'))

@app.route('/teacher/export')
@login_required
def export_csv():
    if current_user.role != 'teacher': return redirect(url_for('login'))
    log_audit(current_user.id, 'Exported CSV')
    with get_db() as conn:
        data = conn.execute('''SELECT u.username, q.title, a.score, a.xp_earned, a.time_taken, a.completed_at FROM attempts a JOIN users u ON a.user_id=u.id JOIN quizzes q ON a.quiz_id=q.id ORDER BY a.completed_at DESC''').fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Username', 'Quiz', 'Score (%)', 'XP', 'Temps (s)', 'Date'])
    for row in data: writer.writerow([row['username'], row['title'], row['score'], row['xp_earned'], row['time_taken'], row['completed_at']])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode('utf-8')), mimetype='text/csv', as_attachment=True, download_name='algoedu_export.csv')

# ==========================================
# API & ERROR HANDLING
# ==========================================
@app.route('/api/notifications/read', methods=['POST'])
@login_required
def mark_notifications_read():
    with get_db() as conn: conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (current_user.id,))
    return jsonify({'ok': True})



@app.errorhandler(404)
def not_found(e):
    return render_template_string('{% extends "base.html" %}{% block content %}<div class="text-center" style="padding: 100px;"><div style="font-family: Orbitron; font-size: 6rem; color: var(--neon-pink);">404</div><p style="color: var(--text-muted);">Introuvable</p><a href="/" class="btn-neon btn mt-3">← Retour</a></div>{% endblock %}'), 404

if __name__ == '__main__':
    init_db()
    logger.info("AlgoEdu V4 (PPE Enhanced) starting...")
    app.run(debug=True, threaded=True)