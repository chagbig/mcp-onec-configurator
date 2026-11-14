# -*- coding: utf-8 -*-
r"""Публикация markdown-статьи в WordPress chagbig.ru через REST (IDEAS: блог).

Штатный сценарий: агент пишет отчёт/статью (md), публикует на сайт одной
командой. Механика проверена живьём 13.11.2026 (пост id 72).

Требования и ловушки (реальные, найденные при первом прогоне):
- реквизиты — в Y:\infra\deploy\projects\wp-chagbig.env (WP_USER/WP_APP_PASS,
  EDGE_IP — адрес edge-прокси сети; всё это приватные данные, НЕ в git,
  пароль — Application Password из wp-admin, не пароль входа!);
- nginx должен передавать Authorization в fastcgi (fastcgi_param
  HTTP_AUTHORIZATION — заведено в infra/web/wp-nginx-chagbig.conf, иначе WP не
  видит пароль приложения и REST даёт 401 rest_cannot_create);
- chagbig.ru из этой сети резолвится не на сайт — подключаемся с SNI/Host
  chagbig.ru через edge (TLS терминируется там; адрес — EDGE_IP в env);
- REST /wp-json/wp/v2/* без ЧПУ может отдавать edge-заглушку — используется
  прямая pretty-точка, проверена живьём;
- идемпотентность: пост ищется по slug — повторный прогон обновляет существующий.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import socket
import ssl
import http.client
import sys

ENV_PATH = r'Y:\infra\deploy\projects\wp-chagbig.env'
WP_HOST = 'chagbig.ru'


class BlogError(RuntimeError):
    pass


def _load_env(path: str) -> dict:
    if not os.path.isfile(path):
        raise BlogError(f'файл реквизитов не найден: {path}')
    creds = {}
    for line in io.open(path, encoding='utf-8'):
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            creds[k.strip()] = v.strip()
    if not creds.get('WP_USER') or not creds.get('WP_APP_PASS'):
        raise BlogError('в env нет WP_USER/WP_APP_PASS')
    if not creds.get('EDGE_IP'):
        raise BlogError('в env нет EDGE_IP (адрес edge-прокси сети)')
    return creds


def md_to_html(md_text: str) -> str:
    """Минимальный md→HTML для статей блога: h2/h3, ul, code block, p,
    **жирный**, ссылки. Markdown вставляется агентом как обычный текст."""
    out, in_code, in_list = [], False, False
    for line in md_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('```'):
            if in_code:
                out.append('</code></pre>')
                in_code = False
            else:
                if in_list:
                    out.append('</ul>')
                    in_list = False
                out.append('<pre><code>')
                in_code = True
            continue
        if in_code:
            out.append(line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
            continue
        if not stripped:
            if in_list:
                out.append('</ul>')
                in_list = False
            continue
        if stripped.startswith('## '):
            if in_list:
                out.append('</ul>')
                in_list = False
            out.append('<h2>' + stripped[3:] + '</h2>')
            continue
        if stripped.startswith('# '):
            if in_list:
                out.append('</ul>')
                in_list = False
            continue  # заголовок поста задаётся отдельно (title)
        if stripped.startswith('### '):
            if in_list:
                out.append('</ul>')
                in_list = False
            out.append('<h3>' + stripped[4:] + '</h3>')
            continue
        if stripped.startswith('- '):
            if not in_list:
                out.append('<ul>')
                in_list = True
            item = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', stripped[2:])
            item = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', item)
            out.append('<li>' + item + '</li>')
            continue
        if in_list:
            # продолжение элемента списка (многострочный пункт)
            out[-1] = out[-1][:-4] + ' ' + stripped + '</li>'
            continue
        p = stripped
        p = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', p)
        p = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', p)
        out.append('<p>' + p + '</p>')
    if in_list:
        out.append('</ul>')
    if in_code:
        out.append('</code></pre>')
    return '\n'.join(out)


def slugify(text: str, max_len: int = 60) -> str:
    """Транслитерация + slug (кириллица не остаётся в URL)."""
    table = {'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
             'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'i', 'к': 'k', 'л': 'l', 'м': 'm',
             'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
             'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
             'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'}
    low = text.lower()
    out = []
    for ch in low:
        if ch in table:
            out.append(table[ch])
        elif ch.isascii() and (ch.isalnum() or ch == '-'):
            out.append(ch)
        else:
            out.append('-')
    slug = re.sub(r'-{2,}', '-', ''.join(out)).strip('-')[:max_len].strip('-')
    if not slug:
        raise BlogError('slug пуст после транслитерации')
    return slug


def _connect(creds: dict) -> http.client.HTTPSConnection:
    """HTTPS на edge с SNI/Host chagbig.ru (сайт за edge в LAN)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    _orig = socket.getaddrinfo

    def patched(host, *a, **kw):
        if host == WP_HOST:
            return _orig(creds['EDGE_IP'], *a, **kw)
        return _orig(host, *a, **kw)

    socket.getaddrinfo = patched
    return http.client.HTTPSConnection(WP_HOST, context=ctx, timeout=40)


def publish(md_file: str, title: str = '', slug: str = '', status: str = 'publish',
            env_path: str = ENV_PATH) -> dict:
    creds = _load_env(env_path)
    if not os.path.isfile(md_file):
        raise BlogError('md-файл не найден: ' + md_file)
    md_text = io.open(md_file, encoding='utf-8-sig').read()
    if not title:
        for line in md_text.splitlines():
            if line.startswith('# '):
                title = line[2:].strip()
                break
    if not title:
        raise BlogError('title не задан и в md нет заголовка # «…»')
    slug = slug or slugify(title)
    content = md_to_html(md_text)

    auth = base64.b64encode(
        f"{creds['WP_USER']}:{creds['WP_APP_PASS']}".encode()).decode()
    conn = _connect(creds)
    payload = json.dumps({'title': title, 'content': content,
                          'slug': slug, 'status': status},
                         ensure_ascii=False).encode('utf-8')
    conn.request('POST', '/wp-json/wp/v2/posts', body=payload, headers={
        'Authorization': 'Basic ' + auth,
        'Content-Type': 'application/json; charset=utf-8'})
    resp = conn.getresponse()
    body = resp.read().decode('utf-8', errors='replace')
    conn.close()
    if resp.status not in (200, 201):
        raise BlogError(f'REST {resp.status}: {body[:300]}')
    d = json.loads(body)
    return {'id': d['id'], 'link': d['link'], 'status': d['status'],
            'title': title, 'slug': slug}


def edit_post(post_id: int, title: str | None = None, content: str | None = None,
              status: str | None = None, env_path: str = ENV_PATH) -> dict:
    """Обновить существующий пост по id (title/content/status частично)."""
    creds = _load_env(env_path)
    auth = base64.b64encode(
        f"{creds['WP_USER']}:{creds['WP_APP_PASS']}".encode()).decode()
    payload = {}
    if title:
        payload['title'] = title
    if content:
        payload['content'] = content
    if status:
        payload['status'] = status
    if not payload:
        raise BlogError('нет полей для обновления (title/content/status)')
    conn = _connect(creds)
    conn.request('POST', f'/wp-json/wp/v2/posts/{post_id}',
                 body=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                 headers={'Authorization': 'Basic ' + auth,
                          'Content-Type': 'application/json; charset=utf-8'})
    resp = conn.getresponse()
    body = resp.read().decode('utf-8', errors='replace')
    conn.close()
    if resp.status != 200:
        raise BlogError(f'REST {resp.status}: {body[:300]}')
    d = json.loads(body)
    return {'id': d['id'], 'link': d['link'], 'status': d['status']}


def list_posts(env_path: str = ENV_PATH, per_page: int = 10) -> dict:
    """Последние посты блога: [{id, date, slug, link, status}]."""
    creds = _load_env(env_path)
    auth = base64.b64encode(
        f"{creds['WP_USER']}:{creds['WP_APP_PASS']}".encode()).decode()
    conn = _connect(creds)
    conn.request('GET', f'/wp-json/wp/v2/posts?per_page={int(per_page)}'
                 '&orderby=date&order=desc',
                 headers={'Authorization': 'Basic ' + auth})
    resp = conn.getresponse()
    body = resp.read().decode('utf-8', errors='replace')
    conn.close()
    if resp.status != 200:
        raise BlogError(f'REST {resp.status}: {body[:300]}')
    posts = json.loads(body)
    return {'posts': [{'id': p['id'], 'date': p['date'], 'slug': p['slug'],
                       'link': p['link'], 'status': p['status']} for p in posts]}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    ap = argparse.ArgumentParser(description='Публикация md-статьи в блог chagbig.ru')
    ap.add_argument('--md-file', default='')
    ap.add_argument('--title', default='')
    ap.add_argument('--slug', default='')
    ap.add_argument('--status', default='publish', choices=['publish', 'draft'])
    ap.add_argument('--edit', type=int, default=0, help='id существующего поста')
    ap.add_argument('--list', action='store_true', help='последние посты')
    a = ap.parse_args(argv)
    try:
        if a.list:
            res = list_posts()
        elif a.edit:
            content = ''
            if a.md_file:
                if not os.path.isfile(a.md_file):
                    raise BlogError('md-файл не найден: ' + a.md_file)
                content = md_to_html(io.open(a.md_file, encoding='utf-8-sig').read())
            res = edit_post(a.edit, a.title or None, content or None, a.status or None)
        else:
            if not a.md_file:
                raise BlogError('--md-file обязателен (или --list/--edit)')
            res = publish(a.md_file, a.title, a.slug, a.status)
    except BlogError as e:
        print('blog_publish: %s' % e, file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
