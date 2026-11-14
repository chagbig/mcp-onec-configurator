# -*- coding: utf-8 -*-
"""Публикация витрины на GitHub (двойное хранилище, №26-инфра).

GitLab (origin) — полное хранилище со всей историей. GitHub (github) —
витрина: orphan-снапшот текущего дерева main. История GitLab НЕ публикуется
(в ранних коммитах — бизнес-имена и дистрибутив снегопата).

Порядок (автоматизирует то, что делалось вручную для v1.4.0):
1. проверка чистоты дерева;
2. санация бизнес-имён (gh_public_prep.py — правит файлы, коммит в main);
3. обновить ветку public-main на СВЕЖИЙ orphan-снапшот дерева main
   (снапшот = санитизированное дерево, история не публикуется);
4. push github public-main:main --force;
5. опционально: тег v* → push тега (теги указывают на полную историю —
   тег в GitHub вести НЕ на полную историю, а на снапшот-коммит ветки).

Замечания:
- .gitignore должен исключать out/, probe/, spike/, snegopat-client-*.zip,
  .gitlab-ci.yml (уже настроено);
- перед пушем — проверка чистоты git grep по бизнес-именам.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GITIGNORE_MARKERS = ['out/', 'probe/', 'spike/', 'snegopat-client-']
BUSINESS_MARKERS = ['ГисМарк', 'гисмарк', 'УТАП', 'Альбион', 'КЭДО', 'Нефтехимия',
                    'НХ_', '192.168.99.', 'gitlab.chagbig']


def git(args: str | list, cwd: Path = REPO) -> tuple[int, str]:
    argv = ['git'] + (args if isinstance(args, list) else args.split())
    r = subprocess.run(argv, cwd=cwd, capture_output=True,
                       encoding='utf-8', errors='replace', timeout=300)
    return r.returncode, ((r.stdout or '') + (r.stderr or '')).strip()


SNAPSHOT_MSG = "публичная витрина: снапшот текущего дерева main"


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='Публикация витрины на GitHub')
    ap.add_argument('--skip-push', action='store_true',
                    help='только подготовка ветки public-main, без push')
    a = ap.parse_args(argv)

    # 1. дерево чистое
    rc, out = git('status --porcelain')
    if rc != 0 or out:
        print('git-status:', out[:400])
        print('дерево не чистое — сначала закоммитьте работу')
        return 2

    # 2. гитигнор-маркеры на месте
    gi = (REPO / '.gitignore').read_text(encoding='utf-8')
    missing = [m for m in GITIGNORE_MARKERS if m not in gi]
    if missing:
        print('.gitignore не содержит:', ', '.join(missing))
        return 2

    # 3. проверка чистоты бизнес-имен в отслеживаемых файлах
    #    (списком: паттерны через -e; кавычки в строке давали rc=128 и гвард молчал;
    #     сам release_github.py исключён — в нём список маркеров)
    rc, out = git(['grep', '-l', *sum([['-e', m] for m in BUSINESS_MARKERS], []),
                   '--', ':!server/tools/release_github.py'])
    if rc == 0 and out:
        print('ПРИВАТНЫЕ МАРКЕРЫ в файлах (сначала санация):')
        print(out[:1000])
        return 2
    if rc >= 2:
        print('git grep не выполнился (rc=%s) — проверить гвард: %s' % (rc, out[:200]))
        return 2

    # 4. обновить public-main на свежий orphan-снапшот дерева main
    rc, out = git('checkout main')
    if rc != 0:
        print(out)
        return 2
    rc, out = git('checkout --orphan public-main-new')
    if rc != 0:
        print(out)
        return 2
    git('add -A')
    rc, out = git(['commit', '-m', SNAPSHOT_MSG])
    if rc != 0 and 'nothing to commit' not in out:
        print(out)
        return 2
    git('branch -D public-main')
    git('branch -m public-main-new public-main')

    if a.skip_push:
        print('public-main готов (push пропущен по --skip-push)')
        return 0

    # 5. force-push витрины
    rc, out = git('push github public-main:main --force')
    print(out[-300:])
    return 0 if rc == 0 else 2


if __name__ == '__main__':
    sys.exit(main())
