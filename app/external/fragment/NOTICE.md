# Fragment: сторонний код

`_vendor/` — урезанная копия [fragment-api-py](https://github.com/S1qwy/fragment-api-py)
версии **12.1.0** (31.08.2026), автор S1qwy, лицензия MIT (заявлена в метаданных пакета на PyPI).

## Почему копия, а не зависимость

- Начиная с 13.0.0 библиотека отправляет 0,5% каждой TON-покупки на зашитый адрес автора
  и авторизуется на Fragment через общий кошелёк с публичной seed-фразой. 12.1.0 этого не делает.
- 12.1.0 требует `marketapp-api` (режим «без KYC» через сторонний сервис), который нам не нужен.

## Что взято

| Файл | Источник | Изменения |
|---|---|---|
| `exceptions.py` | `FragmentAPI/exceptions.py` | удалён `MarketAppAPIError` |
| `constants.py` | `FragmentAPI/types/constants.py` | удалены токен MarketApp по умолчанию и методы No-KYC |
| `models.py` | `FragmentAPI/types/models.py` | оставлены модели, нужные для покупки звёзд |
| `html.py` | `FragmentAPI/utils/html.py` | оставлен разбор цен на звёзды |
| `http.py`, `proxy.py`, `retry.py`, `decoder.py` | `FragmentAPI/utils/*` | только пути импорта |
| `wallet.py` | `FragmentAPI/utils/wallet.py` | повторная отправка перевода — только при HTTP 429 (иначе возможна двойная оплата) |

Во всех файлах заменены пути импорта `FragmentAPI.*` → `app.external.fragment._vendor.*`.
Клиент (`../client.py`) написан заново по мотивам `FragmentAPI/client.py` и
`FragmentAPI/methods/purchase.py`: только звёзды, только вход по cookies аккаунта с KYC.

Код в `_vendor/` исключён из `ruff`, чтобы его можно было сравнивать с оригиналом.

## Лицензия MIT

Copyright (c) S1qwy

Permission is hereby granted, free of charge, to any person obtaining a copy of this software
and associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
