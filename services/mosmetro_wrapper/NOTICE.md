# MosMetro adapter

This service deliberately contains only the two endpoints required by SubwayRun:
`/mosmetro/schema` and `/mosmetro/route`. It is a small adapter around the
undocumented public endpoints used by the official Moscow Metro applications.

The integration was initially validated against the MIT-licensed
[RailwaysTimetableTelegram](https://github.com/ZhuravlevX/RailwaysTimetableTelegram)
project by ZhuravlevX and KryptonFox. Its Telegram, Troika, MosTrans and account
features are not included here.

The upstream Metro endpoints are undocumented. The service is best-effort and
must not be treated as an official timetable or availability guarantee.
