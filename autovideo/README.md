# autovideo（試作）

VREWを使わず、章PDF＋ページ別台本テキストから、AIナレーション音声＋字幕焼き込みの動画を無料・全自動で作る試作スクリプト。

## 使い方

```bash
cd autovideo
python3 -m venv .venv && .venv/bin/pip install pymupdf edge-tts
.venv/bin/python make_video.py --pdf "../report/assets/02_第2章 集客の基本.pdf" --script test/script_ch2.txt --out out/
```

出力: `out/<PDF名>.mp4`（字幕焼き込み済み）と `out/<PDF名>.srt`。

台本フォーマットは `【ページN】` 見出し＋その下の本文行（例は `test/script_ch2.txt`）。
声・速度は `--voice ja-JP-NanamiNeural`（既定）/ `--rate +0%` で変更可能。

## テスト結果（2026-09-02実施）

- 入力: 「02_第2章 集客の基本.pdf」（6ページ）＋自作テスト台本 `test/script_ch2.txt`
- 出力: `out/02_第2章 集客の基本.mp4`
- 尺: 90.00秒（1920x1080, h264/aac）／ページ数6／1ページ平均15.00秒
- `ffprobe`で解像度・尺を確認、シーン検出（`select='gt(scene,0.01)'`）で16回の変化を検出しページ切替を確認済み
- つまずいた点: ffprobeのバージョン差で`-of default=...nokeys=1`が使えず、JSON出力(`-of json`)に変更して解決

## 既知の限界

- 読み方の細かい調整はSSML未対応。単純な文字列置換辞書（`yomi_dict.json`）でSTEP1→ステップ1等のみ対応。
- 字幕は句点「。」区切りの比例配分（またはedge-ttsの単語境界情報からの概算）で、厳密なタイミング一致ではない。
- 長い1文はlibassが自動改行しないため、画面幅を超えることがある（台本側で文を短く切るのが実用上の回避策）。
- ページの画像は白背景中央寄せ。PDFのアスペクト比が16:9でない場合は上下または左右に白帯が入る。
