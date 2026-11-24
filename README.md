# CMS API

[Create: Media Systems](https://github.com/haruyq/Create-Media-Systems)向けの多機能API

> [!WARNING]
> このプロジェクトは100%AIによって出力されました。  
> 自己責任で使用してください。

## インストール

### Docker

**ソースからビルドする**
```bash
git clone https://github.com/haruyq/CMS-API.git
cd CMS-API

cp .env.example .env

docker compose up -d --build
```

---

**イメージから起動する**

`compose.yaml`
```yml
services:
  api:
    image: ghcr.io/haruyq/cms-api:latest
    ports:
      - "8000:8000"
    env_file:
      - path: .env
        required: false
    environment:
      YTDLP_UPDATE_HOURS: ${YTDLP_UPDATE_HOURS:-24}
    volumes:
      - data:/data
    restart: unless-stopped

volumes:
  data:
```

`.env`
```bash
ALLOWED_HOSTS=youtube.com,youtu.be,discordapp.com,discordapp.net,tver.jp
COOKIE_REFRESH_MINUTES=60
MAX_STREAMS=64
YTDLP_UPDATE_HOURS=24
RESOLVE_CACHE_SECONDS=1800
P2P_SERVER_TOKEN=YOUR_RANDOM_TOKEN_HERE
P2P_STUN_URLS=stun:stun.cloudflare.com:3478
```

```bash
docker compose up -d --build
```

### uv

[Python](https://www.python.org/downloads/)と[uv](https://docs.astral.sh/uv/)が環境にインストールされている場合、直接CMS-APIを起動できます。

```bash
git clone https://github.com/haruyq/CMS-API.git
cd CMS-API

cp .env.example .env

uv sync --frozen --no-dev
uv run --no-sync --env-file .env uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws-max-size 65536
```

## 設定

|  名前  |  既定値  |  内容  |
|   ---   |   ---   |   ---   |
| `ALLOWED_HOSTS` | `...` | APIが取得対象とするホスト(サブドメイン含む) |
| `MAX_STREAMS` | `32` | 最大ストリーム数 |
| `RESOLVE_CACHE_SECONDS` | `1800` | 解決したURLをキャッシュする時間 |
| `YTDLP_UPDATE_HOURS` | `24` | [yt-dlp](https://github.com/yt-dlp/yt-dlp)の更新を確認する間隔 |
| `COOKIE_REFRESH_MINUTES` | `60` | Cookieを更新する間隔(分) |
| `EXTENSION_URL` | なし | 拡張APIのベースURL |
| `EXTENSION_TOKEN` | なし | 拡張APIの認証トークン |
| `EXTENSION_PUBLIC_URL` | なし | 拡張APIがインターネットに公開しているURL |
| `P2P_SERVER_TOKEN` | なし | P2P通信に利用するランダムなトークン |
| `P2P_STUN_URLS` | `stun:stun.cloudflare.com:3478` | P2P通信に使用するSTUNサーバー |

## YouTubeにログイン

> [!WARNING]
> ログインにはバーナーアカウントを使用してください。  
> 最悪の場合、アカウントが禁止される可能性があります。

ログインが必要な動画を再生するために、YouTubeアカウントのCookieをAPIに設定できます。
利用規約と各動画の著作権を遵守して使用してください。これらを使用して発生した損害に対して、開発者は責任を負いません。

1. ブラウザのプライベートウィンドウでYouTubeにログインし、拡張機能でyoutube.comのcookies.txtを書き出す
2. ブラウザを閉じる
3. cookies.txtを`/data/cookies.txt` (`COOKIE_FILE`) に配置する

## TVer

このAPIを経由することで、[TVer](https://tver.jp)をゲーム内で再生することができます。  
ただし、TVerは日本国内に制限されているため、利用する場合はAPIを日本に設置するか、Proxyなどを使用する必要があります。

## 拡張API

このAPIが対応していないサイトを、別に動かす拡張API経由で再生できます。  
映像と音声は拡張APIからプレイヤーへ直接送られ、このAPIは経由しません。

1. `.env`に`EXTENSION_URL`、`EXTENSION_TOKEN`、`EXTENSION_PUBLIC_URL`を設定する
2. 拡張APIで再生するサイトのホストを、`ALLOWED_HOSTS`とMod側の`allowedHosts`に追加する
3. APIを再起動する

拡張APIもDockerで動かす場合は、外部ネットワーク`cms`で接続します。  
ネットワークがない場合は`docker network create cms`で作成してください。

```bash
docker compose -f compose.yaml -f compose.extension.yaml up -d
```

### 要件

| エンドポイント | 呼び出し元 | 内容 |
|   ---   |   ---   |   ---   |
| `GET /patterns` | API | 再生できるURL全体に一致する正規表現を`{"urls": [...], "live": [...]}`で返す。`live`はそのうちライブのもの |
| `POST /resolve` | API | `{"url": ...}`の再生を始め、`{"id": 32桁の16進数, "title", "live", "duration": 秒 or null}`を返す。失敗時は`{"detail": "プレイヤーに表示する理由"}` |
| `GET /video` | プレイヤー | HLSを返す |
| `GET /stream` | プレイヤー | 音声を返す。形式はこのAPIの`/stream`と同じ |

APIからのリクエストには`Authorization: Bearer <EXTENSION_TOKEN>`が付きます。  
プレイヤーは`/video`と`/stream`にリダイレクトされ、URLには元のクエリに加えて`exp`(24時間後のUnix時刻)と`sig`が付きます。  
`sig`は、`EXTENSION_TOKEN`を鍵とした`<エンドポイント名>\n<url>\n<exp>`のHMAC-SHA256(16進数)です。拡張APIは`exp`と`sig`を確認してください。

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
