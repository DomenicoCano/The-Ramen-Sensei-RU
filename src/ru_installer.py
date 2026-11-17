"""Русификатор The Ramen Sensei (неофициальный).

Не содержит файлов игры: читает ваши собственные файлы в KairoGames_Data,
расшифровывает таблицы текста из resources.assets, подставляет русский перевод
и сохраняет резервную копию resources.assets.orig.

Режимы:
  TheRamenSensei_RU_Installer.exe                         - окно установщика
  TheRamenSensei_RU_Installer.exe "<путь>\\KairoGames.exe" - тихий режим для Steam:
                                                            "<путь>\\TheRamenSensei_RU_Installer.exe" %command%
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

import UnityPy

for _s in (sys.stdout, sys.stderr):
    if _s is not None and hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

BASE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
EXE_NAME = 'KairoGames.exe'
DATA_DIR = 'KairoGames_Data'
FILES = ['resources.assets']
DEFAULT_COMPANY = 'Kairosoft'
DEFAULT_PRODUCT = 'こだわりラーメン館'
CYR_RE = re.compile(r'[\u0400-\u04FF]')


class Unsupported(Exception):
    pass


# ---------------------------------------------------------------- game folder

def read_app_info(game_dir):
    path = os.path.join(game_dir, DATA_DIR, 'app.info')
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'rb') as f:
            lines = f.read().decode('utf-8', 'replace').splitlines()
        return [ln.strip() for ln in lines if ln.strip()]
    except OSError:
        return None


def is_game_dir(p):
    if not (os.path.isfile(os.path.join(p, EXE_NAME)) and
            os.path.isfile(os.path.join(p, DATA_DIR, 'resources.assets'))):
        return False
    info = read_app_info(p)
    if info is not None:
        joined = ' '.join(info)
        if DEFAULT_PRODUCT not in joined and 'the ramen sensei' not in joined.lower():
            return False
    return True


def steam_libraries():
    libs = []
    try:
        import winreg
        for hive, key, val in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                               (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath')):
            try:
                with winreg.OpenKey(hive, key) as k:
                    libs.append(winreg.QueryValueEx(k, val)[0])
            except OSError:
                pass
    except ImportError:
        return []
    out = []
    for root in libs:
        out.append(root)
        vdf = os.path.join(root, 'steamapps', 'libraryfolders.vdf')
        try:
            txt = open(vdf, encoding='utf-8', errors='replace').read()
            out += [m.replace('\\\\', '\\') for m in re.findall(r'"path"\s+"([^"]+)"', txt)]
        except OSError:
            pass
    seen, res = set(), []
    for p in out:
        p = os.path.normpath(p)
        if p.lower() not in seen:
            seen.add(p.lower())
            res.append(p)
    return res


def find_game():
    cands = []
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
    for p in (here, os.path.dirname(here), os.path.join(here, 'game'), os.path.join(os.path.dirname(here), 'game')):
        cands.append(p)
    for lib in steam_libraries():
        common = os.path.join(lib, 'steamapps', 'common')
        try:
            for d in os.listdir(common):
                low = d.lower()
                if 'ramen' in low and 'sensei' in low:
                    cands.append(os.path.join(common, d))
        except OSError:
            pass
    for c in cands:
        if is_game_dir(c):
            return c
    return ''


# ---------------------------------------------------------------- Kairosoft archive & XOR

def parse_archive(d):
    if len(d) < 12:
        raise ValueError('short archive')
    H, DS, N = struct.unpack_from('>III', d, 0)
    if not (0 < N < 100000) or 4 + H + DS != len(d):
        raise ValueError('not a kairo archive')
    p = 12
    names = []
    for _ in range(N):
        n = struct.unpack_from('>I', d, p)[0]
        p += 4
        names.append(d[p:p + n].decode('utf-8', 'replace'))
        p += n
    offs = struct.unpack_from('>%dI' % N, d, p)
    p += 4 * N
    sizes = struct.unpack_from('>%dI' % N, d, p)
    p += 4 * N
    base = 4 + H
    flags = d[p:base]
    ents = []
    for nm, o, s in zip(names, offs, sizes):
        q = base + o
        ln = struct.unpack_from('>I', d, q)[0]
        if ln != s:
            raise ValueError('entry size mismatch')
        ents.append((nm, d[q + 4:q + 4 + ln]))
    return ents, flags


def build_archive(ents, flags=None):
    N = len(ents)
    names = b''.join(struct.pack('>I', len(n.encode('utf-8'))) + n.encode('utf-8') for n, _ in ents)
    offs, sizes, o = [], [], 0
    for _, dt in ents:
        offs.append(o)
        sizes.append(len(dt))
        o += 4 + len(dt)
    flags = flags if flags is not None else bytes(N)
    body = names + struct.pack('>%dI' % N, *offs) + struct.pack('>%dI' % N, *sizes) + flags
    hdr = struct.pack('>III', 8 + len(body), o, N) + body
    return hdr + b''.join(struct.pack('>I', len(dt)) + dt for _, dt in ents)


def xor_bytes(data, key):
    if not data:
        return data
    k = (key * (len(data) // len(key) + 1))[:len(data)]
    return (int.from_bytes(data, 'big') ^ int.from_bytes(k, 'big')).to_bytes(len(data), 'big')


def ta_bytes(t):
    s = t.m_Script
    return s.encode('utf-8', 'surrogateescape') if isinstance(s, str) else bytes(s)


def load_template_bytes(raw_bytes):
    raw = raw_bytes.decode('utf-8-sig')
    ents = []
    for line in re.split(r'\r?\n', raw):
        if not line:
            continue
        if line.startswith(',,,'):
            if ents:
                ents[-1]['text'] += '\n' + line[3:]
            continue
        p = line.split(',', 3)
        if len(p) < 4 or not p[0].strip().isdigit():
            continue
        ents.append(dict(id=int(p[0]), table=p[1].lower(), pos=p[2], text=p[3]))
    return ents


def load_format_bytes(raw_bytes):
    fm = {}
    raw = raw_bytes.decode('utf-8-sig')
    for l in raw.splitlines():
        l = l.strip()
        if l:
            n, a, b = l.split(',')
            fm[n.lower()] = (chr(int(a)), chr(int(b)))
    return fm


def strip_tags(s):
    return re.sub(r'<[^>]*>', '', s)


def load_patch_data():
    path = os.path.join(BASE, 'data', 'ru.json')
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def find_assets_texts(env):
    found = {}
    for obj in env.objects:
        if obj.type.name == 'TextAsset':
            t = obj.read()
            if t.m_Name in ('xls', 'language_pack_template_en', 'language_pack_format'):
                found[t.m_Name] = (obj, t)
    for req in ('xls', 'language_pack_template_en', 'language_pack_format'):
        if req not in found:
            raise Unsupported('в resources.assets не найден TextAsset ' + req)
    return found


def find_lang_dir(ents):
    dirs = sorted({
        n.split('/')[0] for n, _ in ents
        if '/' in n and n.lower().endswith('.txt') and n.split('/')[0].lower().startswith('english')
    })
    if not dirs:
        raise Unsupported('в архиве xls не найдена папка English.lproj')
    return dirs[0]


# ---------------------------------------------------------------- state & build

def state(game):
    """'patched' / 'clean' / raises Unsupported"""
    res_path = os.path.join(game, DATA_DIR, 'resources.assets')
    if not os.path.isfile(res_path):
        raise Unsupported('не найден файл resources.assets')
    key = bytes.fromhex(load_patch_data()['key_hex'])
    tdir = tempfile.mkdtemp(prefix='trsru_st_')
    try:
        tmp_res = os.path.join(tdir, 'resources.assets')
        shutil.copyfile(res_path, tmp_res)
        env = UnityPy.load(tmp_res)
        found = find_assets_texts(env)
        xls_bytes = ta_bytes(found['xls'][1])
        del env
    finally:
        shutil.rmtree(tdir, ignore_errors=True)

    try:
        dec = xor_bytes(xls_bytes, key)
        ents, flags = parse_archive(dec)
        if build_archive(ents, flags) != dec:
            raise ValueError('roundtrip mismatch')
    except Exception as e:
        raise Unsupported('не удалось расшифровать архив xls (возможно, другая версия игры): %s' % e)

    langdir = find_lang_dir(ents)
    prefix = langdir + '/'
    for nm, dt in ents:
        if nm == prefix + 'softkey.txt':
            txt = dt.decode('utf-8', 'replace')
            return 'patched' if (CYR_RE.search(txt) or 'Назад' in txt or 'Выход' in txt) else 'clean'
    raise Unsupported('в архиве xls не найден файл softkey.txt')


def apply_tables(tabs_raw, tmpl_bytes, fmt_bytes, data, log):
    ents = load_template_bytes(tmpl_bytes)
    fm = load_format_bytes(fmt_bytes)
    id_to_ru = data.get('id_to_ru', {})
    en_to_ru = data.get('en_to_ru', {})
    extra = {}
    for tbl, items in data.get('extra', {}).items():
        extra[tbl] = {(int(li), int(si)): (cur, rep) for li, si, cur, rep in items}
    renames = data.get('renames', [])

    tabs, seps = {}, {}
    st = {'ok': 0, 'applied': 0, 'loose': 0, 'extra': 0, 'skipped': 0}

    for e in ents:
        t = e['table']
        if t not in fm or t not in tabs_raw:
            continue
        if t not in tabs:
            lc, sc = fm[t]
            raw = tabs_raw[t].decode('utf-8')
            if lc == '\n' and '\r\n' in raw:
                lc = '\r\n'
            seps[t] = (lc, sc)
            tabs[t] = [ln.split(sc) for ln in raw.split(lc)]
        L = tabs[t]
        parts = e['text'].split('\n')

        new = en_to_ru.get(e['text'])
        if new is None:
            new = id_to_ru.get(str(e['id']))
            if new is not None and len(new.split('\n')) != len(parts):
                new = None

        for ps in e['pos'].split('&'):
            li, si = map(int, ps.split('-'))
            if li >= len(L) or si + len(parts) > len(L[li]):
                st['skipped'] += 1
                continue
            cur = L[li][si:si + len(parts)]
            if cur != parts:
                if not all(strip_tags(c) == strip_tags(p) for c, p in zip(cur, parts)):
                    if all(not c.strip() for c in cur):
                        continue
                    ex = extra.get(t, {}).get((li, si))
                    if ex and cur[0] == ex[0]:
                        L[li][si] = ex[1]
                        st['extra'] += 1
                        continue
                    st['skipped'] += 1
                    continue
                st['loose'] += 1
                ex = extra.get(t, {}).get((li, si))
                if ex and cur[0] == ex[0]:
                    L[li][si] = ex[1]
                    st['extra'] += 1
                    continue
            st['ok'] += 1
            if new is None:
                continue
            np_ = new.split('\n')
            if len(np_) != len(parts):
                np_ = (np_ + [''] * len(parts))[:len(parts)] if len(np_) < len(parts) else \
                    np_[:len(parts) - 1] + [' '.join(np_[len(parts) - 1:])]
            L[li][si:si + len(parts)] = np_
            st['applied'] += 1

    for t, cells in extra.items():
        if t not in tabs:
            continue
        L = tabs[t]
        for (li, si), (cur_text, ru_text) in cells.items():
            if li < len(L) and si < len(L[li]) and L[li][si] == cur_text:
                L[li][si] = ru_text
                st['extra'] += 1

    if 'softkey' in tabs and len(tabs['softkey']) > 1 and len(tabs['softkey'][1]) > 1:
        if tabs['softkey'][1][0] == '終了':
            tabs['softkey'][1][1] = 'Выход'

    if st['applied'] < 3000:
        raise Unsupported('версия игры сильно изменилась (совпало только %d строк)' % st['applied'])

    out = {}
    for t, L in tabs.items():
        lc, sc = seps[t]
        text = lc.join(sc.join(s) for s in L)
        for en_name, ru_name in renames:
            if en_name in text:
                text = text.replace(en_name, ru_name)
        out[t] = text.encode('utf-8')

    log('Перевод: %d ячеек по шаблону, %d доп. ячеек, %d пропущено.' % (
        st['applied'], st['extra'], st['skipped']))
    return out


def build(src_dir, log):
    """Build patched resources.assets bytes from clean source in src_dir."""
    data = load_patch_data()
    key = bytes.fromhex(data['key_hex'])

    log('Читаю resources.assets...')
    env = UnityPy.load(os.path.join(src_dir, 'resources.assets'))
    found = find_assets_texts(env)
    xls_obj, xls_ta = found['xls']
    tmpl_bytes = ta_bytes(found['language_pack_template_en'][1])
    fmt_bytes = ta_bytes(found['language_pack_format'][1])

    try:
        dec = xor_bytes(ta_bytes(xls_ta), key)
        ents, flags = parse_archive(dec)
        if build_archive(ents, flags) != dec:
            raise ValueError('archive roundtrip mismatch')
    except Exception as e:
        raise Unsupported('не удалось расшифровать архив xls: %s' % e)

    langdir = find_lang_dir(ents)
    prefix = langdir + '/'
    tabs_raw = {}
    for nm, dt in ents:
        if nm.startswith(prefix) and nm.lower().endswith('.txt'):
            tname = nm[len(prefix):-4].lower()
            tabs_raw[tname] = dt

    if 'softkey' in tabs_raw and CYR_RE.search(tabs_raw['softkey'].decode('utf-8', 'replace')):
        raise Unsupported('исходный файл resources.assets уже русифицирован')

    patched_tabs = apply_tables(tabs_raw, tmpl_bytes, fmt_bytes, data, log)

    new_ents = []
    for nm, dt in ents:
        if nm.startswith(prefix) and nm.lower().endswith('.txt'):
            tname = nm[len(prefix):-4].lower()
            new_ents.append((nm, patched_tabs.get(tname, dt)))
        else:
            new_ents.append((nm, dt))

    blob = xor_bytes(build_archive(new_ents, flags), key)
    xls_ta.m_Script = blob.decode('utf-8', 'surrogateescape')
    xls_ta.save()
    out_res = env.file.save()
    return {'resources.assets': out_res}


# ---------------------------------------------------------------- install / restore

def paths(game):
    d = os.path.join(game, DATA_DIR)
    return {f: os.path.join(d, f) for f in FILES}


def build_from(files, log):
    """Build from a temp copy so UnityPy keeps no handle on target game files."""
    srcdir = tempfile.mkdtemp(prefix='trsru_src_')
    try:
        for f in FILES:
            shutil.copyfile(files[f], os.path.join(srcdir, f))
        out = build(srcdir, log)
        import gc
        gc.collect()
        return out
    finally:
        shutil.rmtree(srcdir, ignore_errors=True)


def set_english_language(game):
    try:
        import winreg
        info = read_app_info(game)
        company = info[0] if info and len(info) >= 1 else DEFAULT_COMPANY
        product = info[1] if info and len(info) >= 2 else DEFAULT_PRODUCT
        key_path = r'Software\%s\%s' % (company, product)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.SetValueEx(k, '_language_pack_h56009614', 0, winreg.REG_BINARY, b'*English\x00')
    except OSError:
        pass


def install(game, log):
    p = paths(game)
    st = state(game)
    if st == 'patched':
        if not all(os.path.isfile(p[f] + '.orig') for f in FILES):
            raise Unsupported('русификация уже стоит, но резервная копия (resources.assets.orig) не найдена. '
                              'Проверьте целостность файлов игры в Steam и запустите установку снова.')
        log('Русификация уже установлена — переустанавливаю из резервной копии.')
        outputs = build_from({f: p[f] + '.orig' for f in FILES}, log)
    else:
        outputs = build_from(p, log)
        log('Сохраняю резервную копию (resources.assets.orig)...')
        for f in FILES:
            shutil.copyfile(p[f], p[f] + '.orig')
    log('Записываю resources.assets...')
    for f in FILES:
        tmpf = p[f] + '.tmp'
        with open(tmpf, 'wb') as fh:
            fh.write(outputs[f])
        os.replace(tmpf, p[f])
    set_english_language(game)
    log('Готово! Язык в игре переключён на English (русский перевод).')


def restore(game, log):
    p = paths(game)
    if state(game) != 'patched':
        log('Игра уже в оригинальном виде.')
        return
    if not all(os.path.isfile(p[f] + '.orig') for f in FILES):
        raise Unsupported('резервная копия (resources.assets.orig) не найдена. '
                          'Используйте в Steam «Проверить целостность файлов».')
    for f in FILES:
        os.replace(p[f] + '.orig', p[f])
    log('Оригинальные файлы восстановлены.')


# ---------------------------------------------------------------- GUI

def run_gui():
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext

    root = tk.Tk()
    root.title('Русификатор The Ramen Sensei')
    root.geometry('640x430')
    root.resizable(False, False)

    tk.Label(root, text='Папка с игрой (где лежит KairoGames.exe):', anchor='w').pack(fill='x', padx=12, pady=(12, 2))
    row = tk.Frame(root)
    row.pack(fill='x', padx=12)
    var = tk.StringVar(value=find_game())
    ent = tk.Entry(row, textvariable=var)
    ent.pack(side='left', fill='x', expand=True)

    def browse():
        d = filedialog.askdirectory(title='Папка с игрой The Ramen Sensei')
        if d:
            var.set(os.path.normpath(d))
    tk.Button(row, text='Обзор...', command=browse).pack(side='left', padx=(6, 0))

    log_box = scrolledtext.ScrolledText(root, height=15, state='disabled')
    log_box.pack(fill='both', expand=True, padx=12, pady=10)

    def log(msg):
        def w():
            log_box.configure(state='normal')
            log_box.insert('end', msg + '\n')
            log_box.see('end')
            log_box.configure(state='disabled')
        root.after(0, w)

    btns = tk.Frame(root)
    btns.pack(pady=(0, 12))

    def work(fn, ok_msg):
        game = var.get().strip().strip('"')
        if not is_game_dir(game):
            messagebox.showerror('Русификатор', 'В этой папке не найдена игра The Ramen Sensei (KairoGames.exe и KairoGames_Data).')
            return
        for b in (b_install, b_restore):
            b.configure(state='disabled')

        def t():
            try:
                fn(game, log)
                root.after(0, lambda: messagebox.showinfo('Русификатор', ok_msg))
            except Unsupported as e:
                log('ОШИБКА: ' + str(e))
                if fn is install:
                    log('Файлы игры не изменены.')
                root.after(0, lambda: messagebox.showerror('Русификатор', str(e)))
            except PermissionError:
                log('ОШИБКА: нет доступа к файлам. Закройте игру и запустите установщик от имени администратора.')
                root.after(0, lambda: messagebox.showerror('Русификатор', 'Нет доступа к файлам игры. Закройте игру и повторите.'))
            except Exception as e:      # noqa
                log('НЕОЖИДАННАЯ ОШИБКА: %r' % e)
                root.after(0, lambda: messagebox.showerror('Русификатор', 'Неожиданная ошибка: %r' % e))
            finally:
                root.after(0, lambda: [b.configure(state='normal') for b in (b_install, b_restore)])
        threading.Thread(target=t, daemon=True).start()

    b_install = tk.Button(btns, text='Установить русификацию', width=26,
                          command=lambda: work(install, 'Русификация установлена. Приятной игры!'))
    b_install.pack(side='left', padx=6)
    b_restore = tk.Button(btns, text='Вернуть английский', width=20,
                          command=lambda: work(restore, 'Оригинальные файлы восстановлены.'))
    b_restore.pack(side='left', padx=6)

    log('Неофициальный русификатор The Ramen Sensei. Закройте игру перед установкой.')
    log('Файлы игры не входят в программу: она правит ваш собственный resources.assets.')
    log('Оригинал сохраняется рядом как resources.assets.orig.')
    if var.get():
        log('Игра найдена: ' + var.get())
    else:
        log('Игра не найдена автоматически — укажите папку вручную.')
    root.mainloop()


def run_launch_mode(exe, args):
    game = os.path.dirname(os.path.abspath(exe))
    try:
        if is_game_dir(game) and state(game) == 'clean':
            install(game, lambda m: None)
    except Exception:       # noqa - never block the game
        pass
    sys.exit(subprocess.call([exe] + args, cwd=game))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith('.exe') and os.path.isfile(sys.argv[1]):
        run_launch_mode(sys.argv[1], sys.argv[2:])
    elif len(sys.argv) > 2 and sys.argv[1] in ('--install', '--restore'):
        g = sys.argv[2]
        try:
            (install if sys.argv[1] == '--install' else restore)(g, print)
        except Unsupported as e:
            print('ERROR:', e)
            sys.exit(1)
    else:
        run_gui()
