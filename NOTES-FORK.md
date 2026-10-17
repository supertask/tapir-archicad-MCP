# フォーク運用メモ（supertask/tapir-archicad-MCP）

元のリポジトリ（upstream）: https://github.com/SzamosiMate/tapir-archicad-MCP（MIT）
フォーク: https://github.com/supertask/tapir-archicad-MCP
ローカル: `C:\Users\voyager2\projects\mw\architect\tapir-archicad-MCP-fork`

## ブランチの役割

| ブランチ | 役割 |
| --- | --- |
| `master` | **フォークの正式版**。フォークの改善を載せ、upstream の更新はここにマージする |
| 短期の作業ブランチ | 新しい改善はブランチで作り、テストが通ったら `master` にマージして消す |
| PR 用ブランチ | 元のリポジトリに PR を出す時だけ、`upstream/master` から切って改善を cherry-pick する |

remote は `origin`（フォーク）と `upstream`（元のリポジトリ）の2つ。`upstream` は push 先を `DISABLED` にしてあり、誤って元のリポジトリに push できない。元のリポジトリの最新は、ローカルのブランチではなく `upstream/master` で参照する。

2026-10-18 に運用を変えた。それまでは `master` を upstream の写しにして、改善は `feature/command-timeouts-and-dialogs` に載せていた。この日に `master` をそのブランチまで fast-forward して、以後は `master` を正式版にした。

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

upstream を取得して `master` にマージし、`uv sync` とテストまで行う。push はしないので、結果を確かめてから `git push origin master` を実行する。

**手作業でやる場合:**

```powershell
$uv = "C:\Users\voyager2\.pyenv\pyenv-win\versions\3.12.3\Scripts\uv.exe"   # PATH の uv は壊れている
git fetch upstream --tags
git switch master
git merge upstream/master          # 衝突したら下の「衝突しやすい所」を見て解消 → git add → git commit
& $uv sync
& $uv run pytest -q
git push origin master
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

## 使用中の環境（2026-10-17 時点）

- upstream v0.6.5（Tapir 1.7.1 対応、multiconn-archicad 0.9.4）を取り込んだ際、`.venv` の `archicad-server.exe` は MCP クライアントが多数起動中で上書きも改名もできなかった。そのため **`.venv-065`** に別途インストールし（`UV_PROJECT_ENVIRONMENT=.venv-065 uv sync`）、設定3か所（`.claude.json`、`.codex\config.toml`、Hermes の `config.yaml`）をそちらの exe に向けた。各ファイルの変更前は `.bak-20261017-tapir065`。
- 古い `.venv`（0.6.4）は、既に起動しているセッションが使い終わるまで残す。全クライアントを閉じた時に `.venv` を同期し直して設定を戻すか、`.venv-065` をそのまま使い続けるかを決める。
- 次回以降の同期でも同じ問題が起きたら、`UV_PROJECT_ENVIRONMENT` で新しいフォルダに入れて設定を向け直す。
- `command_guard.py` の `LONG_RUNNING_COMMANDS` に、v0.6.5 で増えた `project_update_hotlinks` と `project_change_hotlink_nodes` を追加した。

## 追加したツール（2026-10-17）

他の Archicad MCP（davidharutyunyan/archicad-mcp-connector、Boti-Ormandi/archicad-mcp、alesdev88/Archicad-MCP、vitormartins-arq/archicad-mcp-aberto ほか）のコードを読み、Archicad 26 + Windows で独自アドオンなしにできるものを取り入れた。

| ツール | 内容 | ファイル |
| --- | --- | --- |
| `archicad_capture_view` | 必要ならビューを開いてズームし、描画が落ち着くのを待ってから Archicad のウィンドウを PrintWindow で撮って画像で返す。返す画像は長辺 `max_size`（既定 2048、0 で原寸）、原寸 PNG は必ず保存してパスを返す | `capture.py`、`tools/custom/vision_tools.py` |
| `archicad_get_preview_image` | 要素のプレビュー／ゾーンの平面切り抜きを、base64 の文字ではなく画像で返す | `tools/custom/vision_tools.py` |
| `archicad_publish` | パブリッシャーセットを書き出し、できたファイルを列挙し、PDF の先頭ページを画像で返す（pypdfium2） | `tools/custom/vision_tools.py` |
| `archicad_batch` | 複数の命令を1回で順に実行し、1件ずつ結果を返す。全件を先に検証、失敗で停止、`expect_window_type` でウィンドウ違いを防ぐ。各命令はガードを通る | `tools/custom/workflow_tools.py` |
| `archicad_save_project` | 保存後にファイルの更新時刻を確認して、本当に保存できたかを返す | `tools/custom/workflow_tools.py` |
| `archicad_get_guide` | Archicad 26 日本語版の使い方ガイド | `guides/archicad26_guide.md` |
| `archicad_list_commands(query=)` | 命令一覧をキーワードで絞り込み（upstream の `functions.py` への小さな差し込み） | `tools/custom/functions.py` |

- 依存に Pillow と pypdfium2 を追加した（PyMuPDF は AGPL なので使わない）。
- 撮影の解像度は Archicad のウィンドウの大きさで決まる。スライドなどで大きな絵が要る時は `archicad_publish` の PDF を使う。
- 3D のカメラは Tapir 1.7.1 では設定できない。Tapir 本体には `Get/Set3DProjectionSettings` が取り込み済み（2026-10-15、1.7.1 の後）なので、次の版が出たら回転撮影を足す。
