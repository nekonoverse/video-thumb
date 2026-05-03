# video-thumb

動画サムネイル生成マイクロサービス。FFmpeg でフレーム抽出 → Pillow で WebP 変換。

[nekonoverse](https://github.com/nekonoverse/nekonoverse) の動画サムネイル生成ワーカーから呼び出される。

## API

### `POST /thumbnail`

動画ファイルを multipart で受信し、WebP サムネイルを返す。

**リクエスト**: `multipart/form-data`
- `file` (必須): 動画ファイル
- `max_dimension` (任意): サムネイル最大辺 (px)。環境変数 `MAX_DIMENSION` で上限キャップされる

**レスポンス**: WebP 画像バイナリ

**レスポンスヘッダ**:
- `Content-Type: image/webp`
- `X-Video-Duration`: 動画の長さ (秒)
- `X-Video-Width`: 動画の幅 (px)
- `X-Video-Height`: 動画の高さ (px)

### `POST /thumbnail_from_url`

URL から動画を取得し、WebP サムネイルを返す。

**リクエスト**: `application/json`
```json
{
  "url": "https://example.com/video.mp4",
  "max_dimension": 800
}
```

- `url` (必須): 動画 URL。スキームは `http` / `https` のみ許可
- `max_dimension` (任意): サムネイル最大辺 (px)。環境変数 `MAX_DIMENSION` で上限キャップされる

**処理フロー**:

1. Python が `HEAD` でリダイレクトを 200 OK まで追跡し、各ホップで SSRF 検証
2. HEAD が `403/405/501` を返すサーバ (S3 presigned URL 等で起こる) には `GET Range: bytes=0-0` でフォールバックし `Content-Range` からサイズ判定
3. `Content-Length` / `Content-Range` のどちらも取れない / `MAX_FILE_SIZE` を超えるサーバは **400 で拒否**
4. 確定した URL を `ffprobe` / `ffmpeg` に直接渡す。`-protocol_whitelist http,https,tcp,tls`、`-rw_timeout` (μs)、`-max_redirects 0` を必ず付与
5. `-ss N -i URL` で HTTP Range シーク → 必要なフレーム周辺だけダウンロードしてサムネイル生成

**SSRF 対策**: デフォルトでプライベート/ループバック/リンクローカル IP 宛の URL を拒否する。リダイレクト先も同様に検証される。ffmpeg 自身のリダイレクト追従も `-max_redirects 0` で無効化済み (HEAD で検証した終端 URL から GET 時に別ホストへ向けられる経路を遮断)。内部サービスからの利用などで許可したい場合は `ALLOW_PRIVATE_URL=1` を設定。

**残存リスク**: ffmpeg の HTTP GET レスポンスサイズに対する厳密な上限は存在せず、悪意サーバが HEAD で `Content-Length: 1` を申告して GET で大量バイトを流す攻撃には `URL_FETCH_TIMEOUT` (`-rw_timeout` の時間ベース) でしか防御できない。また DNS rebinding (HEAD と GET で異なる IP に解決される) に対しても完全な防御は実装されていない。これらは時間制限が許容できる用途 (内部 S3 連携など) を想定した設計。

**レスポンス**: `/thumbnail` と同じ。

### `GET /health`

ヘルスチェック。FFmpeg の利用可能状態を返す。

## 環境変数

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `MAX_DIMENSION` | 800 | サムネイル最大辺 (px)。リクエスト時の `max_dimension` の上限値も兼ねる |
| `WEBP_QUALITY` | 80 | WebP 品質 (0-100) |
| `SEEK_PERCENT` | 10 | フレーム抽出位置 (動画の何%地点) |
| `MIN_SEEK_SEC` | 1.0 | 最小シーク位置 (秒) |
| `MAX_SEEK_SEC` | 10.0 | 最大シーク位置 (秒) |
| `MAX_FILE_SIZE` | 524288000 | 最大ファイルサイズ (500MB)。`/thumbnail_from_url` では HEAD の `Content-Length` で事前判定 |
| `ALLOW_PRIVATE_URL` | (未設定) | `1` のとき `/thumbnail_from_url` でプライベート IP 宛 URL を許可 |
| `URL_FETCH_TIMEOUT` | 60 | URL ダウンロードタイムアウト (秒) |
| `UDS_PATH` | (未設定) | 設定時は TCP の代わりに Unix Domain Socket でリッスン |

## 起動

```bash
# ローカル (FFmpeg が必要)
pip install -r requirements.txt
uvicorn main:app --port 8005

# Docker
docker build -t video-thumb .
docker run -p 8005:8005 video-thumb
```

## 開発・テスト

ユニットテスト (ffmpeg/ffprobe が必要):

```bash
pip install -r requirements-dev.txt
pytest
```

E2E テスト (Docker と稼働中コンテナを利用):

```bash
docker build -t video-thumb:test .
docker run -d --name vt -p 8005:8005 \
  -e ALLOW_PRIVATE_URL=1 \
  --add-host=host.docker.internal:host-gateway \
  video-thumb:test

# 動画 fixture をホスト側で配信
(cd tests/fixtures && python3 -m http.server 8765 &)

E2E_BASE_URL=http://localhost:8005 \
E2E_VIDEO_URL=http://host.docker.internal:8765/sample.mp4 \
  pytest -m e2e tests/e2e/ -v

docker rm -f vt
```

## nekonoverse との統合

```yaml
# docker-compose.yml
video-thumb:
  image: ghcr.io/nekonoverse/video-thumb:latest
  expose:
    - "8005"

app:
  environment:
    VIDEO_THUMB_URL: http://video-thumb:8005
```

## ライセンス

MIT
