# フォーク運用メモ（supertask/tapir-archicad-MCP）

元のリポジトリ（upstream）: https://github.com/SzamosiMate/tapir-archicad-MCP（MIT）
フォーク: https://github.com/supertask/tapir-archicad-MCP
ローカル: `C:\Users\voyager2\projects\mw\architect\tapir-archicad-MCP-fork`

## ブランチの役割

| ブランチ | 役割 |
| --- | --- |
| `master` | upstream の master の**写し**。フォーク独自のコミットは載せない |
| `feature/command-timeouts-and-dialogs` | フォークの改善（下記）。**実際に使うのはこのブランチ** |

remote は `origin`（フォーク）と `upstream`（元のリポジトリ）の2つ。`upstream` は push 先を `DISABLED` にしてあり、誤って元のリポジトリに push できない。

## このフォークで足したもの

Archicad がモーダルダイアログ（例:「欠落しているアドオン」）を出していると命令を処理せず、MCP が無期限に待ち続ける問題への対策。

- 送信前に `API.IsAlive`（ダイアログ中でも即答する）で確認し、ダイアログが出ていれば**命令を送らずに**、タイトル・本文・ボタン名つきのエラーを返す
- 命令ごとの待ち時間の上限（通常 60 秒、開く・保存・パブリッシュ等は 900 秒、命令別に変更可）。実行中にダイアログが 10 秒続いたら待つのをやめる
- 待つのをやめた命令は Archicad 内部に残り、ダイアログを閉じると実行される。これを「保留中」として記録し、後から実行されたか（成功/失敗）も記録する。同じ書き込み命令の再送は拒否する（`allow_duplicate=true` で強制送信、`archicad_clear_pending_commands` で記録を消去）
- 追加ツール: `archicad_get_dialogs`、`archicad_list_pending_commands`、`archicad_clear_pending_commands`
- 設定（CLI 引数 / 環境変数）は README の「Configuration Options」を参照

取り込みやすくするため、upstream のファイルへの変更は最小限にしてある。追加分はほぼ新しいファイルに分けた:

- 新規: `src/tapir_archicad_mcp/command_guard.py`、`dialogs.py`、`tools/custom/guard_tools.py`、`tests/unit/test_command_guard.py`、このメモ、`scripts/sync-upstream.ps1`
- upstream ファイルへの小さな差し込み: `app.py`、`context.py`、`server.py`、`tools/custom/__init__.py`、`tools/custom/functions.py`（`archicad_call_tool` と `_execute_tool`）、`tests/conftest.py`、テスト2本、`README.md`、`manifest.json`

## upstream の更新を取り込む手順

**簡単な方法（スクリプト）:**

```powershell
cd C:\Users\voyager2\projects\mw\architect\tapir-archicad-MCP-fork
powershell -ExecutionPolicy Bypass -File scripts\sync-upstream.ps1
```

upstream を取得し、`master` を upstream に合わせ、作業ブランチにマージして、`uv sync` とテストまで行う。push はしないので、結果を確かめてから `git push origin master feature/command-timeouts-and-dialogs` を実行する。

**手作業でやる場合:**

```powershell
$uv = "C:\Users\voyager2\.pyenv\pyenv-win\versions\3.12.3\Scripts\uv.exe"   # PATH の uv は壊れている
git fetch upstream --tags
git switch master;  git merge --ff-only upstream/master
git switch feature/command-timeouts-and-dialogs
git merge upstream/master          # 衝突したら下の「衝突しやすい所」を見て解消 → git add → git commit
& $uv sync
& $uv run pytest -q
git push origin master feature/command-timeouts-and-dialogs
```

**衝突しやすい所:** `tools/custom/functions.py` の `archicad_call_tool`（`allow_duplicate` 引数と `check_duplicate` の呼び出し）と `_execute_tool`（`guard.preflight` と `guard.run` で `_dispatch_command` を包む部分）。upstream がここを書き換えたら、upstream 側の新しい形に合わせて、この2か所の差し込みだけを入れ直す。

**注意:**
- `uv sync` は、MCP クライアント（Claude Code / Codex / Hermes）が `archicad-server.exe` を実行中だと、exe を上書きできずに失敗することがある。その時はクライアントを閉じてからやり直す。
- 取り込んだ後は、各クライアントを再起動しないと新しいコードが読み込まれない。
- 新しい「時間のかかる命令」が upstream に増えたら、`command_guard.py` の `LONG_RUNNING_COMMANDS` に追加する。

## 使っている場所（設定ファイル）

フォーク版の実行ファイル: `C:\Users\voyager2\projects\mw\architect\tapir-archicad-MCP-fork\.venv\Scripts\archicad-server.exe`

- `C:\Users\voyager2\.claude.json`
- `C:\Users\voyager2\.codex\config.toml`
- `C:\Users\voyager2\AppData\Local\hermes\config.yaml`

元の実行ファイル（旧版 v0.5.3）は `C:\Users\voyager2\projects\mw\architect\tapir-archicad-MCP\.venv\Scripts\archicad-server.exe`。元に戻すときは、各設定のパスをこちらに戻すか、`*.bak-<日時>` のバックアップを戻す。
