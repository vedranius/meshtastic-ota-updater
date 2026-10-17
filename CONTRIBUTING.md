# Contributing

Contributions are welcome. For most changes, fork the repository, create a focused branch, commit your work, and open a pull request against `main`. Include a short explanation of the behavior changed and the hardware or browser conditions relevant to the change.

## Local development

- Serve the Pages project with `npx wrangler pages dev public`.
- Pages Functions live in `functions/`; static assets live in `public/`.
- The companion agent is in `companion/`; Python 3.10+ is required.
- Never commit Wi-Fi passwords, OTA passwords, access tokens, private keys, or device configuration exports.

Run the connection concurrency regressions with:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/identity-requests.test.cjs
```

These checks use simulated device operations and never flash hardware. When changing the companion, rebuild `public/meshtastic-companion.zip` from the four files in `companion/` and keep the download URL version in `public/index.html` aligned with the agent version.

For changes to firmware flashing, include the target board, firmware version, manifest partition geometry, and the exact image checksum in the pull request description. Do not claim hardware validation unless the updated image was tested on that hardware.
