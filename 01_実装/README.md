# Jev 単勝自動購入

`99_read/Claude.md` の仕様に沿って、JRA の単勝を自動購入する実装です。原型は `旧実装/`(オッズ監視 `scripts/odds/monitor_pre_race_odds.py`、IPAT 操作 `keiba-auto-bet/`)。

## 処理の流れ

| # | 内容 | 実装 |
|---|------|------|
| 1〜3 | レース 30 分前 / 10 分前 / 4 分前に単勝オッズを取得(JRA 公式 → 失敗時 netkeiba) | `odds_fetcher.py`, `auto_bet_main.py` |
| 4 | 3 時点のオッズから勝率の高い馬を Jev で判定 | `jev_judge.py`, `jev_client.py` |
| 5 | 購入対象の単勝を 100 円購入(IPAT) | `ipat_client.py` |
| 6 | 成否・購入時オッズ・購入金額を `ログ.txt` に追記 | `bet_logger.py` |

## Jev による判定

Jev は [TypeSafe AI](https://docs.typesafe.ai/introduction) の「決定専用モデル」です。文章は生成せず、選択(Choice)・採点(Score)・真偽の確率(Noul)だけを返します(`POST https://api.typesafe.ai/v1/systemone`、モデル `jev-latest`)。[解説記事](https://www.ai-crew-school.jp/blog/jev-system-one-model/)

公式ドキュメントで **Jev 1.13 は数値計算・大小比較が苦手**、**英語が主**、**無関係な情報が精度を下げる** とされているため、役割を分けています。

| 担当 | 内容 |
|------|------|
| コード | 1/オッズの正規化(暗黙勝率)、30分前→4分前のオッズ変化率、人気順位、`shortening`(売れた) / `drifting`(離れた) / `stable` の判定(±10%、旧実装の「売れた/離れた」と同じ) |
| Jev | 上を英語の表(馬番のみ・馬名なし)で受け取り、「最も勝ちそうな馬」を Choice で 1 頭選ぶ。各馬の確率と確信度が返る |
| コード | Jev の答えが下の購入条件を **すべて** 満たすかを最終判定 |

| 購入条件 | 既定値 | オプション |
|----------|--------|------------|
| Jev の確信度 | 0.5 以上 | `--min-confidence` |
| Jev が返した勝率(選択確率) | 25% 以上 | `--min-win-prob` |
| 4 分前オッズ | 1.5 〜 10.0 倍 | `--min-odds` `--max-odds` |
| 期待値(Jev 勝率 × 4 分前オッズ) | 1.0 以上 | `--min-ev` |
| 選ばれた馬の market_trend | `drifting` でない | `--trend-pct`(±何 % で判定するか) |

- Jev API が失敗した場合(429/529 等)は、購入可能な時間が残っている間、20 秒ごとに再試行します。最後まで失敗したら見送り(購入しない)です。
- 4 分前のオッズが無いレースは Jev を呼ばず見送ります。30 分前 / 10 分前が取れなかった場合は、取れた時点だけで判定します。
- **閾値は初期値で、収支は検証していません。** 特に「期待値 1.0 以上」は厳しく、ほとんど買わない可能性があります。`dry-run` で数日ログを見て調整してください。Jev の勝率が実際の勝率と合っているかも未検証です。
- 判定を差し替えるときは `jev_judge.judge()` を置き換えれば足ります。

## セットアップ

```powershell
cd 01_実装
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium        # JRA 公式オッズの取得に使用
copy .env.example .env             # TYPESAFE_API_KEY(全モード)と IPAT の認証情報(live / prepare-only)を記入
```

- Jev の API キーは <https://console.typesafe.ai/settings/keys> で発行します。Jev は早期アクセス中(2026-09 時点、ウェイトリスト制)です。
- `../../keiba-scraping`(トップ階層のライブラリ)を直接参照します。
- `.env` が無い場合は `旧実装/keiba-auto-bet/.env` を探します。
- Chrome が必要です(レース一覧の取得と IPAT 操作)。

## 使い方

必ず `dry-run` → `prepare-only` → `live` の順で試してください。

```powershell
# 1. 判定だけ(既定)。IPAT は開かず購入もしない。Jev API は呼ぶ(TYPESAFE_API_KEY が必要)
python auto_bet_main.py --mode dry-run

# 2. IPAT にログインし、購入予定リストにセットするところまで(購入は確定しない)
python auto_bet_main.py --mode prepare-only --tracks 阪神 --races 11

# 3. 実購入
python auto_bet_main.py --mode live --max-daily-bet 1000
```

主なオプション: `--jev-model`(既定 `jev-latest`)  `--date` `--tracks` `--races` `--amount`(既定 100) `--max-daily-bet`(既定 3000) `--min-minutes-to-bet`(既定 1.5) `--poll-seconds`(既定 20) `--once` `--headless` `--log-file`。`--help` で全一覧。

## 03_取得ログ/

オッズを取得するたび(30分前/10分前/4分前)、`../03_取得ログ/取得オッズ_YYYYMMDD.csv` に日付・競馬場・レース番号・取得タイミング・馬番・馬名・単勝オッズ・取得元(JRA/netkeiba)を1頭1行で追記する(`odds_logger.py`)。判定に使う前の生オッズを残すためのログで、購入結果とは別に、Jev の勝率が実際の勝率と合っているかの検証などに使う。

## ログ.txt

1 行 1 件、追記形式。

```
2026-09-19 09:41:12 | 結果=購入成功 | 阪神1R | 単勝 3番 ○○○ | 購入時オッズ=2.6倍 | 購入金額=100円
2026-09-19 09:56:03 | 結果=見送り | 中山2R | 単勝 5番 △△△ | 購入時オッズ=3.4倍 | 購入金額=0円 | 見送り: 5番 期待値 0.812 < 1.0
```

結果の種類: `購入成功` / `購入失敗` / `購入結果不明(要IPAT確認)` / `見送り` / `DRY-RUN(未購入)` / `PREPARE-ONLY(未購入)`。「購入時オッズ」は 4 分前に取得したオッズです(確定オッズは発走時に決まるため、実際の払戻とは異なります)。

## 安全策

- `--mode` 既定は `dry-run`。`live` を明示しない限り購入しません。
- 1 日の購入上限(`--max-daily-bet`)を超える購入はしません。
- 購入ボタン押下後に完了を確認できなかった場合は「購入結果不明」とし、**再試行しません**(二重購入防止)。購入済みとみなして上限にも算入します。IPAT の投票履歴で確認してください。
- `state/state_YYYYMMDD_<mode>.json` に処理状況を保存し、再起動しても同じレースを二重購入しません。やり直したいときはこのファイルを削除します。
- 認証情報は `.env`(`.gitignore` 済み)のみ。

## 未検証・注意点

- **Jev API は実際には呼んでいません**(API キーが無いため)。リクエスト・レスポンスの形式は公式ドキュメント([API](https://docs.typesafe.ai/api.md)、[Cloudflare の Jev 仕様](https://developers.cloudflare.com/ai/models/typesafe/jev/))に合わせ、偽サーバでの動作(認証エラー・429/529 の再試行・応答形式の検証)をテストしています。初回はキーを設定して `--mode dry-run --once` で応答を確認してください。
- `prepare-only` / `live` は実際の IPAT に対して未検証です(認証情報が無いため)。画面操作の要素指定は旧実装のものを移植しており、IPAT の画面変更で動かなくなる可能性があります。初回は `prepare-only` を `--headless` なしで目視確認してください。
- IPAT の発売締切は発走の 1〜2 分前です(要確認)。4 分前の取得 → 判定 → 購入までの時間が短いため、購入用セッションは発走 12 分前から事前ログインして保持します。それでも間に合わない場合は「購入失敗(締切)」としてログに残ります。
- 4 分前オッズの取得許容は 1.5 分です(`config.py` の `SNAPSHOT_TARGETS`)。起動が遅れて取得できなかったレースは見送りになります。
- `旧実装/keiba-auto-bet` は `models.py` / `exceptions.py` が欠けており、そのままでは import できません。本実装では IPAT 操作を `ipat_client.py` に取り込んでいます。

## テスト

```powershell
python -m pytest tests -q
```

Jev クライアント(`jev_client`。リクエスト形式・再試行・エラー処理)、判定ロジック(`jev_judge`。State の組み立てと購入条件)、時系列動作(偽の時計・偽の IPAT セッションで購入/失敗/結果不明/再起動/上限/事前ログイン)、IPAT ページ本文の判定を確認します。ネットワーク・実ブラウザ・実際の Jev API は使いません。

## ファイル構成

```
01_実装/
├── auto_bet_main.py   メイン(監視ループ・CLI)
├── jev_judge.py       Jev 判定(State の組み立て + 購入条件)
├── jev_client.py      Jev API クライアント(Choice)
├── odds_fetcher.py    レース一覧・単勝オッズ・馬名の取得
├── ipat_client.py     IPAT 単勝購入(Selenium)
├── bet_logger.py      ログ.txt への追記
├── config.py          パス・タイミング・判定パラメータ
├── tests/
├── requirements.txt / .env.example / .gitignore
└── ログ.txt, state/   実行時に生成
```
