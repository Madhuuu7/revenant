# Fonts served with the console

Two typefaces are served from `web/public/fonts` beside the bundle rather than
fetched from a font host. The console is shown on free instances that have
just woken up and on machines with no network, and a page that needs a CDN to
look like itself looks wrong exactly when it is being looked at. Latin subsets
only, about 55 KB in total.

Both are licensed under the SIL Open Font License, Version 1.1, which permits
this. Their copyright notices, as required:

- **Public Sans** — Copyright © 2015 Impallari Type, with Reserved Font Name
  Libre Franklin; Public Sans is a fork maintained by the United States
  government. <https://github.com/uswds/public-sans>
  It is a form face, drawn for government paperwork, which is the voice this
  console is written in.
- **DM Mono** — Copyright © 2020 The DM Mono Project Authors.
  <https://github.com/googlefonts/dm-mono>
  Used only for what the runtime itself wrote: event names, idempotency keys,
  state hashes, boundary labels.

The licence text is at <https://openfontlicense.org/>. Neither font is
modified; the files are the Google Fonts latin subsets.
