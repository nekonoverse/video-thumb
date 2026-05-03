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

**SSRF 対策**: デフォルトでプライベート/ループバック/リンクローカル IP 宛の URL を拒否する。リダイレクト先も同様に検証される。内部サービスからの利用などで許可したい場合は `ALLOW_PRIVATE_URL=1` を設定。

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
| `MAX_FILE_SIZE` | 524288000 | 最大ファイルサイズ (500MB) |
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

```bash
pip install -r requirements-dev.txt
pytest
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
