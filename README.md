# CMS API

[Create: Media Systems](https://github.com/haruyq/Create-Media-Systems)向けの音声解決API  

## 起動

```bash
cp .env.example .env
```

```bash
docker compose up -d --build
```

## YouTubeにログイン

ログインが必要な動画を再生するために、自分のアカウントのCookieをAPIに設定できます。

各サイトの利用規約と著作権を守って利用してください。

1. ブラウザで`http://<host>/auth/youtube`を開き、パスワードに`ADMIN_TOKEN`を入力
2. YouTubeにログインし、cookies.txtを貼り付けて保存
3. 状態が「ログイン済み」になれば完了

コマンドも利用できます。

```bash
curl -u admin:$ADMIN_TOKEN --data-binary @cookies.txt http://localhost:8000/auth/youtube
```

> [!WARNING]
> ログインには専用のGoogleアカウントを使用してください。  
> 最悪の場合、アカウントが禁止される可能性があります。

## 拡張API

このAPIが扱わないページを別に動かす拡張APIで再生できます。

```
EXTENSION_URL=http://<拡張のホスト>:<ポート>
EXTENSION_TOKEN=<拡張と同じトークン>
EXTENSION_PUBLIC_URL=http://<プレイヤーから届く拡張のホスト>:<ポート>
```

拡張APIもDockerで動かす場合は `docker compose -f compose.yaml -f compose.extension.yaml up -d` で起動します。`compose.extension.yaml` は外部ネットワーク `cms` を使うので、先に拡張APIを起動するか `docker network create cms` で作成してください。

### 拡張APIの要件

`/patterns` と `/resolve` は、このAPIが `Authorization: Bearer <EXTENSION_TOKEN>` を付けて呼びます。`/video` と `/stream` にはプレイヤーがリダイレクトで送られ、そのURLには元のクエリに加えて `exp`（期限のUnix時刻、24時間後）と `sig` が付きます。`sig` は、`EXTENSION_TOKEN` を鍵にした `<エンドポイント名>\n<url>\n<exp>`（`\n` は改行）のHMAC-SHA256（16進数）です。

| エンドポイント | 用途 |
|---|---|
| `GET /patterns` | 再生できるページのURL全体に一致する正規表現 `{"urls": [...], "live": [...]}` `live` はそのうちライブのもの |
| `POST /resolve` | `{"url": …}` の再生を始め、`{"id": 32桁の16進数, "title", "live", "duration": 秒 or null}` を返す。失敗したら `{"detail": "プレイヤーに見せる理由"}` |
| `GET /video` | プレイヤー向けのHLS。`exp` と `sig` を確かめる |
| `GET /stream` | プレイヤー向けの音声（このAPIの `/stream` と同じ形式）。`exp` と `sig` を確かめる |

## 設定

| 名前 | 既定値 | 内容 |
|---|---|---|
| `ADMIN_TOKEN` | なし | ログイン画面のパスワード |
| `ALLOWED_HOSTS` | `youtube.com,youtu.be,discordapp.com,discordapp.net,tver.jp,cctv.com,cntv.cn,cntv.com,bilibili.com,b23.tv,iwara.tv` | 取得可能にするホスト。Mod側の `allowedHosts` と統一してください |
| `COOKIE_REFRESH_MINUTES` | `60` | Cookieを更新する間隔（分） |
| `MAX_STREAMS` | `32` | 同時に配信するストリーム数。超過時は `503` を返す |
| `RESOLVE_CACHE_SECONDS` | `1800` | 解決したURLを使い回す秒数 |
| `YTDLP_UPDATE_HOURS` | `24`（Compose） | yt-dlpを更新する間隔（時間）。`0` で無効 |
| `COOKIE_FILE` | `/data/cookies.txt` | Cookie の保存先 |
| `EXTENSION_URL` | なし | [拡張](#拡張API)のURL |
| `EXTENSION_TOKEN` | なし | 拡張との認証用トークン |
| `EXTENSION_PUBLIC_URL` | なし | プレイヤーから届く拡張のURL |

## 注意点

### 1. リクエストと配信

* **重複処理の最適化**: 同一URLへの同時リクエストは1回に統合され、結果はキャッシュされます。
* **YouTubeの待ち時間**: 広告付き動画は、広告スキップ可能時間（数秒程度）が経過した後に `/stream` の配信が始まります。
* **接続切断の対応**: 再生中の接続が切れた場合、ffmpeg は即座に停止します。

### 2. TVer 配信の挙動

* **通常番組とライブの対応**: 通常番組は yt-dlp、非対応のライブ配信は本API自身で解析します。
* **国内限定**: TVer は日本国内制限があるため、API サーバーは日本国内への設置が必須です。
* **ライブの同期・タイムシフト再生**: TVerライブ配信で `/video` と共通の `session` を指定すると、映像の再生開始位置から連動した追っかけ再生（`start`指定）が可能です。
* **帯域節約とキャッシュ**: 音声は最低画質のストリームから取得して受信量を抑えます。データはメモリ上にキャッシュされ、複数接続時もTVerへのリクエストは1本分に集約されます。

### 3. 運用・セキュリティ上の注意点

* **セキュリティ（認証なし）**: `/stream` と `/info` には認証が存在しないため、公開時はファイアウォール等でMinecraftサーバーおよびプレイヤーIP以外からのアクセスを遮断してください。
* **プロセス構成**: キャッシュやCookieをメモリ共有するため、1プロセスで動作させる必要があります。
* **アクセス制限**: 許可されていないホストへのURLリクエストは `403` になります。

## 開発

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run uvicorn app.main:app --reload
```

## ライセンス

このプロジェクトは[MIT License](./LICENSE)で公開されています。
