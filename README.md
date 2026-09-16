# そら保存

家族・知人向けに、YouTubeの動画または音声を保存するための小さなWebアプリです。取得処理はyt-dlpのPython API、結合と変換はFFmpegが担当します。

このアプリはログインした利用者だけが使える限定公開を前提にしています。利用者は管理者が発行し、一般登録やYouTubeアカウントのCookie取り込みは行いません。保存するコンテンツの権利と、YouTubeおよび地域の規約への適合は利用者が確認してください。

## 構成

```text
ブラウザ → Caddy（HTTPS） → FastAPI
                              ├─ SQLite（利用者・セッション・ジョブ）
                              └─ 共有ボリューム ← worker（yt-dlp / FFmpeg）
                                               ← cleanup（自動削除）
```

画面は日本語の簡単モードを初期表示にし、必要な場合だけ詳細モードを開きます。完成ファイルは準備完了から24時間、履歴は7日間保持します。

## Debianサーバーへの導入

Debian 12以降、2 vCPU、メモリ4GB、空き容量30GB以上を初期想定にしています。Docker EngineとDocker Compose Pluginを先に導入してください。

1. プロジェクトをサーバーへ配置し、設定ファイルを作成します。

   ```sh
   cp .env.example .env
   openssl rand -hex 32
   ```

   表示された値を`.env`の`APP_SECRET_KEY`へ設定し、`DOMAIN`を公開用ホスト名へ変更します。Cloudflare Tunnel経由で公開する場合は、Tunnelのhostnameと同じ値にしてください。

2. 起動します。

   ```sh
   docker compose build --pull
   docker compose up -d
   docker compose ps
   curl -fsS https://downloads.example.com/healthz
   ```

   `downloads.example.com`は設定したホスト名に置き換えてください。このComposeはCaddyをlocalhostの80/443だけにbindし、originでは内部証明書を使います。Cloudflare Tunnel経由ではCloudflare側の公開HTTPSを利用します。

## Cloudflare Tunnelで公開する場合

既存の`cloudflared`設定へ、アプリ用の入口を追加します。Tunnelの設定ファイルに次のようなルートを加え、`<公開ホスト名>`を`.env`の`DOMAIN`と一致させてください。

```yaml
  - hostname: <公開ホスト名>
    service: https://localhost:443
    originRequest:
      noTLSVerify: true
      originServerName: <公開ホスト名>
```

DNSルートをまだ作っていない場合は、Tunnelの認証済み環境で次を実行します。

```sh
cloudflared tunnel route dns <トンネル名またはID> <公開ホスト名>
sudo systemctl restart cloudflared
```

`https://<公開ホスト名>/healthz`が`{"status":"ok"}`を返すことと、ログイン画面が表示されることを確認してください。

3. 最初の管理者を作成します。

   ```sh
   docker compose exec app python -m app.manage create-admin oishi
   ```

   パスワードを指定しない場合は仮パスワードを表示します。管理者は初回ログイン後にパスワードを変更してください。固定の初期パスワードを使う場合は、端末の履歴やプロセス一覧に残らない運用で次のように実行します。

   ```sh
   docker compose exec app python -m app.manage create-admin oishi --password '12文字以上の安全な値'
   ```

管理者画面の「利用者管理」から家族・知人のアカウントを作成できます。初期パスワードを空欄にすると仮パスワードを表示し、本人が初回ログインで変更します。

## 開発・テスト

Python 3.12以上の仮想環境で依存関係を入れます。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
pytest -q
```

yt-dlpの実取得を試す場合は、FFmpeg、Deno、yt-dlp-ejsが利用できる環境で、権利を確認した短い公開動画を使ってください。通常の自動テストは外部YouTubeへ接続しません。

## 運用

処理を一度に実行するworkerは1つです。利用者あたり待機・実行中を3件、全体を20件までに制限します。動画は2時間、1ジョブの最終ファイルは4GiBを上限とし、空き容量が5GiB未満になると新しいジョブを受け付けません。

更新前にデータベースをバックアップします。動画ファイルは短期保存なので、必要な完成ファイルは各利用者の端末へ保存しておきます。

```sh
docker compose stop app worker cleanup
docker run --rm \
  -v yt-downloader_yt_data:/data \
  -v "$PWD:/backup" \
  alpine sh -c 'cp /data/app.db /backup/app.db.backup'
docker compose build --pull
docker compose up -d
```

作業ディレクトリ名が異なる場合、Dockerのボリューム名`yt-downloader_yt_data`を`docker volume ls`で確認して置き換えてください。新しいイメージで問題が出た場合は、更新前のイメージタグまたはCompose設定へ戻し、同じデータボリュームを使って再起動します。DBスキーマを変更する更新では、先にバックアップを取り、起動ログと`/healthz`を確認してください。

ログは次で確認できます。

```sh
docker compose logs -f app worker cleanup caddy
```

管理者向けの詳細な失敗理由は「処理ログ」から見られます。利用者画面には、再試行につながる短い日本語の説明だけを表示します。

## 主要な制限

- YouTubeの動画1本だけに対応します。プレイリスト一括取得、ライブ配信、字幕、区間指定、YouTubeログインは初版対象外です。
- URLはYouTubeの対応ホストと動画IDへ正規化し、任意URL・任意コマンド・任意保存パスは受け付けません。
- セッションCookieはHttpOnly、SameSite=Laxです。本番では`COOKIE_SECURE=true`を設定し、Caddy経由のHTTPSだけで利用してください。
- アプリ、worker、cleanupはroot以外で実行し、コンテナをread-onlyにしています。書き込み先は共有データボリュームと一時領域に限定しています。
