import webview, threading, subprocess, sys, time, os, ctypes, atexit, socket, random, re, traceback

from agentmain import build_llm_clients
from self_supervisor import AutoReplySupervisor

WINDOW_WIDTH, WINDOW_HEIGHT, RIGHT_PADDING, TOP_PADDING = 600, 900, 0, 100
AUTO_IDLE_SECONDS = 1800
AUTO_MIN_INTERVAL_SECONDS = 12
ASK_USER_PATTERNS = (
    'waiting for your answer', 'ask_user', 'please provide input', 'need your input', 'which option',
    '请提供输入', '请选择', '请你确认', '需要你', '请回答', '请先确认', '请先回复', '请回复', '请回复我',
    '你想要', '你希望', '是否需要', '先确认 3 个问题',
)

script_dir = os.path.dirname(os.path.abspath(__file__))
frontends_dir = os.path.join(script_dir, "frontends")
window = None

def find_free_port(lo=18501, hi=18599):
    ports = list(range(lo, hi+1)); random.shuffle(ports)
    for p in ports:
        try: s = socket.socket(); s.bind(('127.0.0.1', p)); s.close(); return p
        except OSError: continue
    raise RuntimeError(f'No free port in {lo}-{hi}')

def get_screen_width():
    try: return ctypes.windll.user32.GetSystemMetrics(0)
    except: return 1920

def start_streamlit(port):
    global proc
    cmd = [sys.executable, "-m", "streamlit", "run", os.path.join(frontends_dir, "stapp.py"), "--server.port", str(port), "--server.address", "localhost", "--server.headless", "true", "--client.toolbarMode", "viewer"]
    proc = subprocess.Popen(cmd)
    atexit.register(proc.kill)

def inject(text):
    if window is None: return
    window.evaluate_js(f"""
        const textarea = document.querySelector('textarea[data-testid="stChatInputTextArea"]');
        if (textarea) {{
            // 1. 用原生 setter 设置值（绕过 React）
            const nativeTextAreaValueSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
            nativeTextAreaValueSetter.call(textarea, {repr(text)});
            // 2. 触发 React 的 input 事件
            textarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
            // 3. 触发 change 事件（有些组件需要）
            textarea.dispatchEvent(new Event('change', {{ bubbles: true }}));
            // 4. 延迟提交
            setTimeout(() => {{
                const btn = document.querySelector('[data-testid="stChatInputSubmitButton"]');
                if (btn) btn.click();
            }}, 200);
        }}""")

def get_ui_state():
    if window is None: return {}
    raw_state = window.evaluate_js("""
        (() => {
            const txt = (id) => {
                const el = document.getElementById(id);
                return el ? (el.textContent || '') : '';
            };
            return {
                last_reply_time: parseInt(txt('last-reply-time') || '0') || 0,
                last_user_prompt: txt('last-user-prompt'),
                last_assistant_reply: txt('last-assistant-reply'),
                streaming: txt('streaming-flag') === '1',
                autonomous_enabled: txt('autonomous-enabled') === '1',
                auto_reply_enabled: txt('auto-reply-enabled') === '1',
                auto_cycle_enabled: txt('auto-cycle-enabled') === '1',
                auto_stop_on_done_enabled: txt('auto-stop-on-done-enabled') === '1'
            };
        })();
    """)
    if raw_state is None:
        return {}
    if isinstance(raw_state, dict): return dict(raw_state)
    try: return dict(raw_state)
    except Exception: return {}

PASTE_HOOK_JS = """if (!window._pasteHooked) { window._pasteHooked = true;
    document.addEventListener('paste', e => {
        const items = e.clipboardData?.items; if (!items) return;
        let t = null, hasText = false;
        for (const item of items) {
            if (item.kind === 'string' && (item.type === 'text/plain' || item.type === 'text/html')) hasText = true;
            if (item.kind === 'file') { t = item.type.startsWith('image/') ? 'image in clipboard, ' : 'file in clipboard, '; }
        }
        if (!t || hasText) return;
        e.preventDefault(); e.stopImmediatePropagation();
        const el = document.querySelector('textarea[data-testid="stChatInputTextArea"]') || document.activeElement;
        if (el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) {
            const s = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
            s.call(el, el.value + t); el.dispatchEvent(new Event('input', { bubbles: true }));
        }
    }, true);
}"""

def looks_like_ask_user(text):
    lowered = (text or '').lower()
    if any(p in lowered for p in ASK_USER_PATTERNS): return True
    stripped = (text or '').strip()
    if not stripped: return False
    if stripped.count('?') + stripped.count('？') >= 2: return True
    if len(re.findall(r'^\s*(?:\d+[\.\u3001\)]|[-*])\s+.+(?:\?|\uFF1F|[:\uFF1A])\s*$', stripped, re.M)) >= 2: return True
    return any(p in stripped for p in ('请先提问', '请先确认', '先确认', '先回答', '请回答', '问题'))

def set_ui_toggle(label_text, enabled):
    if window is None: return
    target = 'true' if enabled else 'false'
    window.evaluate_js(f"""
        (() => {{
            const labels = [...document.querySelectorAll('label')];
            const label = labels.find(el => (el.textContent || '').includes({repr(label_text)}));
            const input = label ? label.querySelector('input[type="checkbox"]') : null;
            if (!input) return false;
            if (input.checked === {target}) return true;
            input.click();
            return true;
        }})();""")

def disable_auto_followups():
    set_ui_toggle("自动代答 ask_user", False)
    set_ui_toggle("自动续跑", False)

def idle_monitor(supervisor):
    last_trigger_time = 0
    last_handled_reply = ''
    while True:
        time.sleep(5)
        try:
            window.evaluate_js(PASTE_HOOK_JS)
            now = time.time()
            if now - last_trigger_time < AUTO_MIN_INTERVAL_SECONDS: continue
            state = get_ui_state()
            if not isinstance(state, dict) or not state: continue
            gs = lambda k, default=None: state[k] if k in state else default
            if gs('streaming'): continue
            last_reply = gs('last_reply_time', 0) or int(time.time())
            last_user_prompt = gs('last_user_prompt', '')
            last_assistant_reply = gs('last_assistant_reply', '')
            autonomous_enabled = gs('autonomous_enabled', False)
            auto_reply_enabled = gs('auto_reply_enabled', False)
            auto_cycle_enabled = gs('auto_cycle_enabled', False)
            auto_stop_on_done_enabled = gs('auto_stop_on_done_enabled', False)
            auto_follow_enabled = auto_reply_enabled or auto_cycle_enabled
            if not last_assistant_reply or last_assistant_reply == last_handled_reply:
                if autonomous_enabled and now - last_reply > AUTO_IDLE_SECONDS:
                    print('[Idle Monitor] Detected idle state, injecting task...')
                    inject("[AUTO]🤖 用户已经离开超过30分钟，作为自主智能体，请阅读自动化sop，执行自动任务。")
                    last_trigger_time = now
                continue
            if supervisor is not None and auto_stop_on_done_enabled:
                should_stop, stop_reason = supervisor.should_auto_stop(last_user_prompt, last_assistant_reply)
                if should_stop:
                    print(f'[Idle Monitor] AI decided to stop auto followups: {stop_reason}')
                    disable_auto_followups()
                    last_trigger_time = now
                    last_handled_reply = last_assistant_reply
                    continue
            if supervisor is not None and auto_reply_enabled and looks_like_ask_user(last_assistant_reply):
                generated, reason = supervisor.generate_next_input(last_user_prompt, last_assistant_reply, None)
                if generated:
                    print(f'[UI AutoReply/{reason}] {generated[:100]}')
                    inject(generated)
                    last_trigger_time = now
                    last_handled_reply = last_assistant_reply
                    continue
            if supervisor is not None and auto_follow_enabled:
                generated, reason = supervisor.generate_next_input(last_user_prompt, last_assistant_reply, {'result': 'CURRENT_TASK_DONE'})
                if generated:
                    print(f'[UI AutoReply/{reason}] {generated[:100]}')
                    inject(generated)
                    last_trigger_time = now
                    last_handled_reply = last_assistant_reply
                    continue
            last_handled_reply = last_assistant_reply
        except Exception as e:
            print(f'[Idle Monitor] Error: {e!r}')
            print(traceback.format_exc())

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('port', nargs='?', default='0'); 
    parser.add_argument('--tg', action='store_true', help='启动 Telegram Bot'); 
    parser.add_argument('--qq', action='store_true', help='启动 QQ Bot');
    parser.add_argument('--feishu', '--fs', dest='feishu', action='store_true', help='启动 Feishu Bot');
    parser.add_argument('--wechat', '--wx', dest='wechat', action='store_true', help='启动 WeChat Bot');
    parser.add_argument('--wecom', action='store_true', help='启动 WeCom Bot');
    parser.add_argument('--dingtalk', '--dt', dest='dingtalk', action='store_true', help='启动 DingTalk Bot');
    parser.add_argument('--sched', action='store_true', help='启动计划任务调度器')
    parser.add_argument('--llm_no', type=int, default=0, help='LLM编号')
    args = parser.parse_args()
    port = str(find_free_port()) if args.port == '0' else args.port
    print(f'[Launch] Using port {port}')
    threading.Thread(target=start_streamlit, args=(port,), daemon=True).start()

    if args.tg:
        tgproc = subprocess.Popen([sys.executable, os.path.join(frontends_dir, "tgapp.py")], creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        atexit.register(tgproc.kill)
        print('[Launch] Telegram Bot started')
    else: print('[Launch] Telegram Bot not enabled (use --tg to start)')

    if args.qq:
        qqproc = subprocess.Popen([sys.executable, os.path.join(frontends_dir, "qqapp.py")], creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        atexit.register(qqproc.kill)
        print('[Launch] QQ Bot started')
    else: print('[Launch] QQ Bot not enabled (use --qq to start)')

    if args.feishu:
        fsproc = subprocess.Popen([sys.executable, os.path.join(frontends_dir, "fsapp.py")], creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        atexit.register(fsproc.kill)
        print('[Launch] Feishu Bot started')
    else: print('[Launch] Feishu Bot not enabled (use --feishu to start)')

    if args.wechat:
        wxproc = subprocess.Popen([sys.executable, os.path.join(frontends_dir, 'wechatapp.py')], creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        atexit.register(wxproc.kill)
        print('[Launch] WeChat Bot started')
    else: print('[Launch] WeChat Bot not enabled (use --wechat to start)')

    if args.wecom:
        wcproc = subprocess.Popen([sys.executable, os.path.join(frontends_dir, "wecomapp.py")], creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        atexit.register(wcproc.kill)
        print('[Launch] WeCom Bot started')
    else: print('[Launch] WeCom Bot not enabled (use --wecom to start)')

    if args.dingtalk:
        dtproc = subprocess.Popen([sys.executable, os.path.join(frontends_dir, "dingtalkapp.py")], creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        atexit.register(dtproc.kill)
        print('[Launch] DingTalk Bot started')
    else: print('[Launch] DingTalk Bot not enabled (use --dingtalk to start)')
    
    if args.sched:
        scheduler_proc = subprocess.Popen([sys.executable, os.path.join(script_dir, "agentmain.py"), "--reflect", os.path.join(script_dir, "reflect", "scheduler.py"), "--llm_no", str(args.llm_no)], creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        atexit.register(scheduler_proc.kill)
        print('[Launch] Task Scheduler started (duplicate prevented by scheduler port lock)')
    else: print('[Launch] Task Scheduler not enabled (--sched)')

    ui_supervisor = None
    try:
        auto_clients = build_llm_clients()
        auto_llm_no = (args.llm_no + 1) % len(auto_clients)
        ui_supervisor = AutoReplySupervisor(auto_clients, auto_llm_no, auto_cycle=True)
        print(f'[Launch] UI supervisor ready with {ui_supervisor.get_llm_name()}')
    except Exception as e:
        print(f'[Launch] UI supervisor unavailable: {e}')

    monitor_thread = threading.Thread(target=idle_monitor, args=(ui_supervisor,), daemon=True)
    monitor_thread.start()
    if os.name == 'nt':
        screen_width = get_screen_width()
        x_pos = screen_width - WINDOW_WIDTH - RIGHT_PADDING
    else: x_pos = 100
    time.sleep(2) 
    window = webview.create_window(
        title='GenericAgent', url=f'http://localhost:{port}',
        width=WINDOW_WIDTH, height=WINDOW_HEIGHT, x=x_pos, y=TOP_PADDING,
        resizable=True, text_select=True)
    webview.start()
