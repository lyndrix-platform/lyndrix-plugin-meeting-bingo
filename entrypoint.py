import os
import random
import time
import json
from uuid import uuid4
from nicegui import ui, app as nicegui_app
from core.api import ModuleManifest
from ui.layout import main_layout 
from ui.theme import UIStyles
# ==========================================
# 1. MANIFEST
# ==========================================
manifest = ModuleManifest(
    id="lyndrix.plugin.bingo",
    name="Meeting Bingo",
    version="0.0.3",
    description="Multiplayer Bullshit-Bingo für langatmige Meetings.",
    author="Lyndrix",
    icon="grid_on",
    type="PLUGIN",
    min_core_version="1.0.0",
    auto_enable_on_install=False,
    repo_url="https://github.com/marvin1309/lyndrix-meeting-bingo",
    ui_route="/bingo",
    permissions={"subscribe": ["vault:ready_for_data"], "emit": []} # FIX: Rechte hinzugefügt
)

# ==========================================
# 2. PLUGIN STATE & DEFAULTS
# ==========================================
plugin_state = {
    "sessions": {}, 
    "scoreboard_enabled": False,
    "scoreboard": {},
    "lobby_last_update": time.time()
}

DEFAULT_TERMS = [
    "Synergieeffekte", "Wir müssen das agil angehen", "Könnt ihr mich hören?", 
    "Ich teile mal meinen Bildschirm", "Werfe ich mal in die Runde", "Lass uns das offline besprechen",
    "Das ist ein valider Punkt", "Da bin ich ganz bei dir", "Können wir das kurz parken?",
    "Haben wir noch genug Puffer?", "Win-Win Situation", "Wir müssen die PS auf die Straße bringen",
    "Da müssen wir nochmal tiefer reinbohren", "Ich war auf Mute", "Sorry, ich hatte Verbindungsprobleme",
    "Nehmen wir mal als Action Item mit", "Best Practice", "Low Hanging Fruits",
    "Das steht nicht auf der Agenda", "Quick Win", "KPIs", "Out of the box", "Am Ende des Tages",
    "Ich muss gleich in den nächsten Call", "Lasst uns das skalieren"
]

SARCASTIC_MESSAGES = [
    "Knapp vorbei ist auch 2. Platz.",
    "Toll gemacht, du bist der erste Verlierer.",
    "Schön für dich. Der Keks ist aber schon weg.",
    "Immerhin hast du teilgenommen.",
    "Silbermedaille im Bullshit-Bingo. Glückwunsch?",
    "Die Goldmedaille für 'Zuspätkommen' geht an dich."
]

# ==========================================
# 3. GAME LOGIC
# ==========================================
def check_win(board, size):
    for i in range(size):
        if all(board[i * size + j]['marked'] for j in range(size)): return True
    for j in range(size):
        if all(board[i * size + j]['marked'] for i in range(size)): return True
    if all(board[i * size + i]['marked'] for i in range(size)): return True
    if all(board[i * size + (size - 1 - i)]['marked'] for i in range(size)): return True
    return False

# ==========================================
# 4. SETTINGS UI
# ==========================================
def render_settings_ui(ctx):
    current_state = {"scoreboard_enabled": plugin_state["scoreboard_enabled"]}

    def apply_save():
        plugin_state["scoreboard_enabled"] = current_state["scoreboard_enabled"]
        ctx.set_secret("scoreboard_enabled", str(current_state["scoreboard_enabled"]))
        ui.notify("Bingo Settings gespeichert.", type="positive")

    with ui.column().classes('w-full gap-4 pt-2'):
        with ui.card().classes(f'{UIStyles.CARD_GLASS} w-full').style('padding: 0; flex-wrap: nowrap'):
            ui.element('div').classes('h-1 w-full bg-gradient-to-r from-emerald-400 via-teal-400 to-green-400')
            with ui.column().classes('w-full flex-grow p-5 gap-4'):
                with ui.row().classes('items-center gap-2 mb-1'):
                    ui.icon('grid_on', size='18px').classes('text-emerald-400')
                    ui.label('Ethik-Einstellungen').classes('text-sm font-bold uppercase tracking-widest text-slate-300')
                with ui.row().classes('w-full items-center gap-4'):
                    ui.switch('Scoreboard aktivieren (Erfasst Gewinner permanent)').bind_value(current_state, 'scoreboard_enabled').props('color=primary')
                ui.label('Tipp: Je weniger Beweise, desto besser für den Ethikcodex!').classes('text-xs text-orange-500 italic')
                with ui.row().classes('w-full justify-end mt-2'):
                    ui.button('Speichern', on_click=apply_save, icon='save', color='primary').props('unelevated rounded size=sm')

# ==========================================
# 5. SETUP & MAIN UI
# ==========================================
def setup(ctx):
    ctx.log.info("STARTUP: Loading Meeting Bingo Plugin...")

    # Standard-Begriffe Datei anlegen
    terms_file = os.path.join(os.path.dirname(__file__), "terms.txt")
    if not os.path.exists(terms_file):
        os.makedirs(os.path.dirname(terms_file), exist_ok=True)
        with open(terms_file, "w", encoding="utf-8") as f:
            f.write("\n".join(DEFAULT_TERMS))

    # --- FIX: AUF DEN VAULT WARTEN ---
    @ctx.subscribe('vault:ready_for_data')
    async def load_data_from_vault(payload=None):
        ctx.log.info("LOAD: Vault ready. Fetching Bingo data...")
        
        sb_enabled = ctx.get_secret("scoreboard_enabled")
        if sb_enabled == "True":
            plugin_state["scoreboard_enabled"] = True

        stored_scoreboard = ctx.get_secret("bingo_scoreboard")
        if stored_scoreboard:
            try:
                plugin_state["scoreboard"] = json.loads(stored_scoreboard)
                ctx.log.info(f"SUCCESS: Loaded {len(plugin_state['scoreboard'])} players from scoreboard.")
            except Exception as e:
                ctx.log.error(f"ERROR: Failed to parse scoreboard: {e}")
                plugin_state["scoreboard"] = {}

    def save_scoreboard_to_vault():
        try:
            ctx.set_secret("bingo_scoreboard", json.dumps(plugin_state["scoreboard"]))
        except Exception as e:
            ctx.log.error(f"ERROR: Vault Save Error (Scoreboard): {e}")

    @ui.page('/bingo')
    @main_layout('Meeting Bingo')
    async def bingo_page():
        ui.add_head_html('<script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.6.0/dist/confetti.browser.min.js"></script>')
        
        local_state = {"session_id": None, "nickname": None}
        
        with open(terms_file, "r", encoding="utf-8") as f:
            file_terms = [line.strip() for line in f.readlines() if line.strip()]

        main_container = ui.column().classes('w-full')

        def render_lobby():
            main_container.clear()
            with main_container:
                with ui.row().classes('w-full justify-between items-center mb-6'):
                    ui.label('Meeting Bingo Lobby').classes('text-2xl font-bold dark:text-zinc-100')

                with ui.row().classes('w-full gap-6 flex-col md:flex-row items-stretch'):
                    
                    # --- SESSION ERSTELLEN ---
                    with ui.card().classes(f'{UIStyles.CARD_GLASS} flex-1').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-indigo-400 via-sky-400 to-cyan-400')
                        with ui.column().classes('w-full flex-grow p-5 gap-3'):
                            ui.label('Neue Session starten').classes('text-lg font-bold text-indigo-400')

                            s_name = ui.input('Session Name').props('outlined dense').classes('w-full')
                            s_size = ui.slider(min=3, max=5, value=3).props('label-always')
                            ui.label('Feldgröße (3x3 bis 5x5)').classes('text-xs text-zinc-500')

                            s_terms = ui.textarea('Bingo Begriffe (Eine Zeile pro Begriff)').props('outlined').classes('w-full')
                            s_terms.value = "\n".join(file_terms)

                            def create_session():
                                terms = [t.strip() for t in s_terms.value.split('\n') if t.strip()]
                                size = int(s_size.value)
                                if len(terms) < size * size:
                                    ui.notify(f'Du brauchst mindestens {size*size} Begriffe!', type='negative')
                                    return
                                if not s_name.value:
                                    ui.notify('Bitte einen Namen vergeben!', type='negative')
                                    return

                                sid = str(uuid4())[:8]
                                plugin_state["sessions"][sid] = {
                                    "name": s_name.value,
                                    "size": size,
                                    "words": terms,
                                    "winners": [],
                                    "players": {},
                                    "last_update": time.time()
                                }
                                plugin_state["lobby_last_update"] = time.time()
                                ui.notify(f'Session {s_name.value} erstellt!', type='positive')

                            ui.button('Session erstellen', on_click=create_session, color='primary').classes('w-full mt-2').props('unelevated rounded')

                    # --- SESSION BEITRETEN ---
                    with ui.card().classes(f'{UIStyles.CARD_GLASS} flex-1').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-emerald-400 via-teal-400 to-green-400')
                        with ui.column().classes('w-full flex-grow p-5 gap-3'):
                            ui.label('Aktiven Sessions beitreten').classes('text-lg font-bold text-emerald-400')

                            default_nick = nicegui_app.storage.user.get('username', 'Gast')
                            p_nick = ui.input('Dein Nickname', value=default_nick).props('outlined dense').classes('w-full')

                            session_list = ui.column().classes('w-full gap-2')
                            local_lobby_time = [0]

                            def join_session(sid):
                                if not p_nick.value:
                                    return ui.notify('Nickname fehlt!', type='warning')

                                sess = plugin_state["sessions"][sid]
                                nick = p_nick.value

                                if nick not in sess["players"]:
                                    sample = random.sample(sess["words"], sess["size"] * sess["size"])
                                    board = [{'word': w, 'marked': False} for w in sample]
                                    sess["players"][nick] = {"board": board, "won": False}
                                    sess["last_update"] = time.time()
                                    plugin_state["lobby_last_update"] = time.time()

                                local_state["session_id"] = sid
                                local_state["nickname"] = nick
                                render_game()

                            def update_lobby_live():
                                if local_state["session_id"] is not None: return
                                if plugin_state["lobby_last_update"] <= local_lobby_time[0]: return

                                local_lobby_time[0] = plugin_state["lobby_last_update"]
                                session_list.clear()
                                with session_list:
                                    if not plugin_state["sessions"]:
                                        ui.label('Keine aktiven Sessions.').classes('text-zinc-500 italic')
                                    for sid, sess in plugin_state["sessions"].items():
                                        with ui.row().classes('w-full items-center justify-between p-3 bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700'):
                                            with ui.column().classes('gap-0'):
                                                ui.label(sess["name"]).classes('font-bold text-sm')
                                                ui.label(f'{len(sess["players"])} Spieler | {sess["size"]}x{sess["size"]}').classes('text-xs opacity-50')
                                            ui.button('Beitreten', on_click=lambda s=sid: join_session(s), color='emerald').props('unelevated rounded size=sm')

                            ui.timer(1.0, update_lobby_live)
                            plugin_state["lobby_last_update"] = time.time()

                # --- SCOREBOARD ---
                if plugin_state["scoreboard_enabled"]:
                    with ui.row().classes('w-full mt-6'):
                        with ui.card().classes(f'{UIStyles.CARD_GLASS} w-full').style('padding: 0; flex-wrap: nowrap'):
                            ui.element('div').classes('h-1 w-full bg-gradient-to-r from-amber-400 via-orange-400 to-yellow-500')
                            with ui.column().classes('w-full flex-grow p-5 gap-3'):
                                with ui.row().classes('items-center gap-2 mb-2'):
                                    ui.icon('emoji_events', size='20px').classes('text-amber-400')
                                    ui.label('Wall of Shame (Scoreboard)').classes('text-xl font-bold text-amber-400')
                            
                            if not plugin_state["scoreboard"]:
                                ui.label('Noch keine Gewinner erfasst. Zeit für das nächste Meeting!').classes('text-zinc-500 italic')
                            else:
                                sorted_scores = sorted(plugin_state["scoreboard"].items(), key=lambda item: item[1], reverse=True)
                                
                                with ui.column().classes('w-full gap-0 rounded-xl overflow-hidden border border-slate-200 dark:border-zinc-700'):
                                    for rank, (player, wins) in enumerate(sorted_scores[:10]):
                                        if rank == 0:
                                            bg = 'bg-emerald-500 dark:bg-emerald-600'
                                            text_color = 'text-white'
                                            win_color = 'text-emerald-100'
                                        else:
                                            bg = 'bg-white dark:bg-zinc-800' if rank % 2 == 0 else 'bg-slate-50 dark:bg-zinc-900/50'
                                            text_color = 'text-slate-800 dark:text-zinc-200'
                                            win_color = 'text-slate-500 dark:text-zinc-400'
                                            
                                        medal = '🥇' if rank == 0 else '🥈' if rank == 1 else '🥉' if rank == 2 else f'{rank+1}.'
                                        
                                        with ui.row().classes(f'w-full justify-between items-center p-3 {bg}'):
                                            ui.label(f'{medal} {player}').classes(f'font-bold {text_color}')
                                            ui.label(f'{wins} Siege').classes(f'font-mono {win_color}')

        def render_game():
            main_container.clear()
            sid = local_state["session_id"]
            nick = local_state["nickname"]
            sess = plugin_state["sessions"][sid]
            size = sess["size"]
            
            with main_container:
                with ui.row().classes('w-full justify-between items-center mb-6'):
                    ui.label(f'Session: {sess["name"]}').classes('text-2xl font-bold dark:text-zinc-100')
                    def leave():
                        local_state["session_id"] = None
                        render_lobby()
                    ui.button('Verlassen', on_click=leave, color='red', icon='logout').props('flat rounded size=sm')

                with ui.row().classes('w-full gap-8 flex-col lg:flex-row items-start'):
                    
                    # --- DEIN BOARD ---
                    with ui.column().classes('w-full lg:w-2/3'):
                        ui.label(f'Dein Board ({nick})').classes('text-lg font-bold mb-2')
                        grid = ui.grid(columns=size).classes('w-full gap-3')
                        
                        def draw_board():
                            grid.clear()
                            board = sess["players"][nick]["board"]
                            has_won = sess["players"][nick]["won"]
                            
                            with grid:
                                for idx, cell in enumerate(board):
                                    if cell['marked'] and has_won:
                                        bg_style = '!bg-emerald-500 border-emerald-400 !text-white shadow-lg shadow-emerald-500/50'
                                    elif cell['marked']:
                                        bg_style = '!bg-indigo-500 border-indigo-400 !text-white shadow-lg'
                                    else:
                                        bg_style = '!bg-white dark:!bg-zinc-800 border-slate-200 dark:border-zinc-700 !text-slate-700 dark:!text-zinc-200 hover:!bg-slate-50 dark:hover:!bg-zinc-700'

                                    with ui.card().classes(
                                        f'h-24 md:h-32 w-full p-2 flex items-center justify-center cursor-pointer transition-all duration-200 active:scale-95 border {bg_style}'
                                    ).on('click', lambda i=idx: mark_cell(i)):
                                        ui.label(cell['word']).classes('text-xs md:text-sm font-bold text-center leading-snug line-clamp-4')
                                    
                            def mark_cell(i):
                                if sess["players"][nick]["won"]: return 
                                sess["players"][nick]["board"][i]['marked'] = not sess["players"][nick]["board"][i]['marked']
                                
                                if check_win(sess["players"][nick]["board"], size):
                                    sess["players"][nick]["won"] = True
                                    if nick not in sess["winners"]:
                                        sess["winners"].append(nick)
                                        if len(sess["winners"]) == 1:
                                            ui.notify('BINGO! 🎉 Du hast das Meeting überlebt!', type='positive', position='center', progress=True)
                                            ui.run_javascript('confetti({particleCount: 150, spread: 100, origin: { y: 0.6 }});')
                                            
                                            # SCOREBOARD UPDATE
                                            if plugin_state["scoreboard_enabled"]:
                                                if nick not in plugin_state["scoreboard"]:
                                                    plugin_state["scoreboard"][nick] = 0
                                                plugin_state["scoreboard"][nick] += 1
                                                save_scoreboard_to_vault()
                                                
                                        else:
                                            msg = random.choice(SARCASTIC_MESSAGES)
                                            ui.notify(msg, type='warning', position='center')
                                            
                                sess["last_update"] = time.time()
                                draw_board() 
                                
                        draw_board()

                    # --- ANDERE SPIELER ---
                    with ui.card().classes(f'{UIStyles.CARD_GLASS} w-full lg:w-1/3 min-h-[300px]').style('padding: 0; flex-wrap: nowrap'):
                        ui.element('div').classes('h-1 w-full bg-gradient-to-r from-violet-400 via-purple-400 to-indigo-400')
                        with ui.column().classes('w-full flex-grow p-5 gap-3'):
                            ui.label('Andere Spieler').classes('text-lg font-bold')
                            players_container = ui.column().classes('w-full gap-4')
                        
                        last_seen_update = [0] 

                        def update_others():
                            if local_state["session_id"] != sid: return 
                            if sess["last_update"] <= last_seen_update[0]: return
                            
                            last_seen_update[0] = sess["last_update"]
                            players_container.clear()
                            
                            with players_container:
                                for p_nick, p_data in sess["players"].items():
                                    if p_nick == nick: continue 
                                    
                                    marks = sum(1 for c in p_data["board"] if c['marked'])
                                    total = size * size
                                    
                                    with ui.column().classes('w-full bg-slate-50 dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800 rounded-xl p-3'):
                                        with ui.row().classes('w-full items-center justify-between mb-2'):
                                            with ui.column().classes('gap-0'):
                                                ui.label(p_nick).classes('font-bold text-sm text-indigo-500')
                                                ui.label(f'{marks}/{total} markiert').classes('text-xs opacity-60')
                                            
                                            if p_data["won"]:
                                                platz = sess["winners"].index(p_nick) + 1
                                                color = "text-amber-500" if platz == 1 else "text-slate-400"
                                                ui.icon('emoji_events', size='20px').classes(color).tooltip(f'{platz}. Platz')
                                            else:
                                                ui.spinner('dots', size='1em').classes('opacity-30')

                                        with ui.grid(columns=size).classes('w-full gap-1'):
                                            for cell in p_data["board"]:
                                                bg = '!bg-indigo-500 !text-white border-indigo-400' if cell['marked'] else '!bg-slate-200 dark:!bg-zinc-800 !text-slate-600 dark:!text-zinc-500 border-slate-300 dark:border-zinc-700'
                                                with ui.card().classes(f'w-full aspect-square p-1 flex items-center justify-center shadow-none border {bg}'):
                                                    ui.label(cell['word']).classes('text-[8px] md:text-[10px] leading-tight text-center line-clamp-3')

                        ui.timer(1.0, update_others)
                        update_others() 

        render_lobby()
        