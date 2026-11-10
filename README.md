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

## P2P通信

プレイヤー同士をWebRTCで直接つなぐためのシグナリングです。APIはオファー・アンサー・ICE候補を中継するだけで、映像・音声・データは通しません。TURN（中継サーバー）には対応していないため、双方のNATが直接接続を許さない環境ではつながりません。

1. `.env` に長いランダムな `P2P_SERVER_TOKEN` を設定します（例: `openssl rand -hex 32`）。同じ値をMinecraftサーバーの `config/cms-server.toml` の `p2pServerToken`（または環境変数 `CMS_P2P_SERVER_TOKEN`）に入れてください。このファイルのほかの設定はプレイヤーに送られますが、`p2pServerToken` だけは送られません。トークンをプレイヤーに渡さないでください
2. インターネット越しに使う場合は `P2P_STUN_URLS` にSTUNサーバーを設定します。同梱のSTUN専用coturnを使う場合は `docker compose --profile stun up -d` で起動し、UDP 3478を開けて `P2P_STUN_URLS=stun:<公開ホスト>:3478` とします。LAN内だけなら空で構いません
3. 外部に公開する場合はリバースプロキシでHTTPS/WSSにしてください。パス接頭辞（例: `https://example.com/cms`）付きでも動きます

帯域は、ホストが視聴者とつながった直後に、その接続で短いデータを送って測ります。再生中も実際の転送速度に合わせて調整します。APIは測定しません。

### プロトコル（バージョン1）

| エンドポイント | 認証 | 内容 |
|---|---|---|
| `GET /p2p/config` | なし | `{"protocol": 1, "enabled", "stun_urls"}` |
| `POST /p2p/sessions` | サーバートークン | `{"host_id": UUID}` でルームを作り、ホストのチケットを返す |
| `POST /p2p/sessions/{id}/peers` | サーバートークン | `{"peer_id": UUID}` で視聴者のチケットを返す。同じプレイヤーの重複とホスト自身は `409` |
| `DELETE /p2p/sessions/{id}` | サーバートークン | ルームを閉じ、全員を切断してチケットを無効にする |
| `DELETE /p2p/sessions/{id}/peers/{peer_id}` | サーバートークン | その視聴者を切断。ホストを指定するとルームを閉じる |
| `WS /p2p/sessions/{id}/signal` | チケット | シグナリング |

サーバートークンとチケットはどちらも `Authorization: Bearer <トークン>` ヘッダーで渡します（クエリ文字列は不可）。DELETEは対象がなくても `204` を返します。CMS 1.2.3以前は`upload_bps`と`download_bps`（1以上100 Gbps以下の整数）も送ります。どちらも省略できます。チケットは `{"protocol": 1, "session_id", "peer_id", "host_id", "role": "host"|"viewer", "token", "stun_urls"}` です。

WebSocketでは、参加者が変わるたびに `{"type": "peers", "host_id", "peers": [{"peer_id", "role"}], "budget_bps"}` が届きます。ホストには接続中の全員、視聴者にはホストだけが見えます。`budget_bps` はCMS 1.2.3以前のための値です。ホストと接続中の全視聴者がbpsを送った場合だけ `floor(min(0.65 × ホストの上り ÷ 接続中の視聴者数, 0.65 × 最も遅い視聴者の下り))` になり、それ以外は `0` です。bpsを送った参加者は `peers` の各要素にも `upload_bps` と `download_bps` が付きます。

クライアントが送るのは次の3種類だけで、これ以外のフィールドは拒否されます。転送時は `to` が送信者の `from` に置き換わります。

* `{"type": "offer", "to", "sdp"}` ホストから視聴者へ
* `{"type": "answer", "to", "sdp"}` 視聴者からホストへ
* `{"type": "ice", "to", "candidate", "sdp_mid", "sdp_mline_index"}` ホストと視聴者の間

問題があると `{"type": "error", "message"}`、ルームが閉じると `{"type": "closed", "message"}` が届きます。

### 制限

* ルームはAPIプロセスのメモリ上にあるため、APIは1プロセス・1台で動かしてください（複数ワーカーや複数台への振り分けは不可）。APIを再起動するとルームは消え、ホストと視聴者はやり直しが必要
* ルームは最大64、1ルームの視聴者は最大16
* チケットは120秒以内にWebSocketで使わないと無効。ホストが接続しなければルームごと閉じる
* 1人1接続。ホストが切断するとルームが閉じ、視聴者が切断すると退出扱い
* メッセージは64 KiB、SDPは48 KiB、ICE候補は2048文字まで。バイナリは不可。`relay` 候補は拒否
* 1接続あたり10秒に64件・128 KiBまで（ホストは接続中の視聴者数倍）。さらに相手1人へ転送できるのも10秒に64件・128 KiBまで。超えるか受信が詰まると切断

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
| `P2P_SERVER_TOKEN` | なし | [P2P通信](#p2p通信)用にMinecraftサーバーとだけ共有する秘密。空なら無効 |
| `P2P_STUN_URLS` | なし | プレイヤーに渡すSTUNサーバー（`stun:` のみ、カンマ区切り） |

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
