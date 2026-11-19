# Changelog

## [0.2.0](https://github.com/brownplaya239/smid-scanner/compare/v0.1.0...v0.2.0) (2026-11-19)


### Features

* **reports:** add a Claude Opus 5.5 analyst take to research reports, with every number inserted from the data ([#25](https://github.com/brownplaya239/smid-scanner/issues/25)) ([f26033c](https://github.com/brownplaya239/smid-scanner/commit/f26033c3e5e8d583bb58f3f34ad159434d41b3ce))
* **scanner:** move every AI feature to the latest Claude models ([#24](https://github.com/brownplaya239/smid-scanner/issues/24)) ([d835b1a](https://github.com/brownplaya239/smid-scanner/commit/d835b1ad4a029f9c4c248fc8d1026ba29f3fb07f))
* **site:** add privacy-friendly visitor analytics, off until a PostHog key is set ([#31](https://github.com/brownplaya239/smid-scanner/issues/31)) ([fe83cd5](https://github.com/brownplaya239/smid-scanner/commit/fe83cd5ef1051483dfe50c95003b52561225060d))
* **site:** explain TickerDesk to new visitors and stop wording that reads like trade advice ([#28](https://github.com/brownplaya239/smid-scanner/issues/28)) ([75d3d6b](https://github.com/brownplaya239/smid-scanner/commit/75d3d6b18ae40f3e6e0099e5869fbedf5bdb9a1b))
* **site:** turn on visitor analytics with the new PostHog project ([#32](https://github.com/brownplaya239/smid-scanner/issues/32)) ([7aea38f](https://github.com/brownplaya239/smid-scanner/commit/7aea38fa16b361223ae9ddcbc564c1a26ac5cd2b))


### Bug Fixes

* **brief:** finish the November stats fixes in the brief, social posts and hedge monitor ([#20](https://github.com/brownplaya239/smid-scanner/issues/20)) ([dfcb696](https://github.com/brownplaya239/smid-scanner/commit/dfcb696cf1abfd80f487eb2e526d3700b3bff630))
* **brief:** make the Daily Brief honest about prices and readable in plain English ([#30](https://github.com/brownplaya239/smid-scanner/issues/30)) ([82d531d](https://github.com/brownplaya239/smid-scanner/commit/82d531dcf7eccdc6b3d4651da4ab849e6850fb83))
* **brief:** make the Daily Brief sign-up forms actually subscribe people ([#21](https://github.com/brownplaya239/smid-scanner/issues/21)) ([56d4667](https://github.com/brownplaya239/smid-scanner/commit/56d4667c7cff4adb69268ed9daa684da00168444))
* **brief:** stop the new Daily Brief layout being blocked almost every morning ([#27](https://github.com/brownplaya239/smid-scanner/issues/27)) ([a237b19](https://github.com/brownplaya239/smid-scanner/commit/a237b19bc354ec5ebd1e9241a44e670c299f7a86))
* **desk:** compare event-only implied moves with mean past reactions ([#15](https://github.com/brownplaya239/smid-scanner/issues/15)) ([06d867c](https://github.com/brownplaya239/smid-scanner/commit/06d867c35a07cc1a45d4b97b8ea2ddd321eee941))
* **desk:** make the remaining AI Trade Desk statistics measure what they say ([#14](https://github.com/brownplaya239/smid-scanner/issues/14)) ([714b609](https://github.com/brownplaya239/smid-scanner/commit/714b60932d4bd64bcdbfa15b8773e0bc5ddea907))
* **reports:** publish live numbers on the transparency and landing pages ([#23](https://github.com/brownplaya239/smid-scanner/issues/23)) ([1a035a2](https://github.com/brownplaya239/smid-scanner/commit/1a035a2c0a5a447eaf56062628c575718164fd64))
* **scanner:** add the missing six dependency so scheduled scans run again ([#5](https://github.com/brownplaya239/smid-scanner/issues/5)) ([cfe947c](https://github.com/brownplaya239/smid-scanner/commit/cfe947cb29006b7b29cb00675681adb0cd4493ec))
* **scanner:** correct options-flow and earnings track-record statistics ([#9](https://github.com/brownplaya239/smid-scanner/issues/9)) ([5356786](https://github.com/brownplaya239/smid-scanner/commit/5356786605cdba522be13ce40051b3d7fea523ec))
* **scanner:** count each flow alert once and model stock costs realistically in the scorecard ([#12](https://github.com/brownplaya239/smid-scanner/issues/12)) ([a4b7599](https://github.com/brownplaya239/smid-scanner/commit/a4b759934d4852eab9d8f26b3359063886ce1be3))
* **scanner:** honest flow side labels, real golden sweeps, DST-safe scans ([#10](https://github.com/brownplaya239/smid-scanner/issues/10)) ([5ca1fd3](https://github.com/brownplaya239/smid-scanner/commit/5ca1fd3c8fbb32bc189ef2c372067dcbbcc5ae25))
* **scanner:** make the Options Flow sub-tab numbers measure what they say ([#13](https://github.com/brownplaya239/smid-scanner/issues/13)) ([52a154b](https://github.com/brownplaya239/smid-scanner/commit/52a154b96d87867accd328fb8ecabe99a58d373c))
* **scanner:** run breakout scans on time and keep the economic and earnings calendars current ([#22](https://github.com/brownplaya239/smid-scanner/issues/22)) ([e1f1191](https://github.com/brownplaya239/smid-scanner/commit/e1f1191722f7ac5eac6ba11a803ab6f7fe59ec24))
* **scanner:** run flow research on the full ledger and build its verdicts from the data ([#11](https://github.com/brownplaya239/smid-scanner/issues/11)) ([48d1b32](https://github.com/brownplaya239/smid-scanner/commit/48d1b32032577f2dbc3d763bb146beb2e40018da))
* **site:** correct duplicate, mislabeled and mixed-timeframe numbers on the Desk ([#29](https://github.com/brownplaya239/smid-scanner/issues/29)) ([2029efa](https://github.com/brownplaya239/smid-scanner/commit/2029efa7d5c2525475dfb8e3b502a833ae7db129))
* **site:** let the research report show on the page instead of a blocked box ([#33](https://github.com/brownplaya239/smid-scanner/issues/33)) ([475ab56](https://github.com/brownplaya239/smid-scanner/commit/475ab56ad7a05450c4f9929c7056e1f1784f7121))
* **site:** make pricing, privacy and terms promise only what TickerDesk delivers ([#18](https://github.com/brownplaya239/smid-scanner/issues/18)) ([880042c](https://github.com/brownplaya239/smid-scanner/commit/880042c140604eaec57c1a64b68a8c70c3574826))
* **site:** stop showing a stalled news wire as live and keep news flowing ([#19](https://github.com/brownplaya239/smid-scanner/issues/19)) ([c4d6606](https://github.com/brownplaya239/smid-scanner/commit/c4d6606a6ba4a6b51f176b160fd6a7490cfe60fd))
* **worker:** give every account a profile so paid upgrades always apply ([#17](https://github.com/brownplaya239/smid-scanner/issues/17)) ([9736a99](https://github.com/brownplaya239/smid-scanner/commit/9736a99b7d38b2d5ff29ac65778a7807365a5724))
* **worker:** stop users granting themselves Premium or unlimited AI reports ([#8](https://github.com/brownplaya239/smid-scanner/issues/8)) ([2e59e53](https://github.com/brownplaya239/smid-scanner/commit/2e59e53ac7d780d6613f76fb7d094c97e9acb3ce))
