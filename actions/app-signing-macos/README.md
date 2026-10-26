# app-signing-macos

Signs a macOS `.app` with a Developer ID and notarizes it, along with its DMG or
zip. It implements `REL-006` from
[docs/policy-general.md](../../docs/policy-general.md#macos-app-signing-and-notarization-candidate-rel-006)
([#462](https://github.com/zackees/ci.yml/issues/462)). The procedure was proven in
obs-rust/obs-studio#2430 and obs-rust/obs-studio#2433, in run 40246027884.

Use it in **release jobs only**. The action refuses to run on `pull_request`,
`pull_request_target` and `merge_group` events, and on a `push` that is not a tag.
Missing secrets fail the job. It never falls back to ad-hoc signing.

## What it does

1. Decodes the `.p12` into `$RUNNER_TEMP` and imports it into a new temporary
   keychain that has a random password. It then confirms that
   `security find-identity -v -p codesigning` lists the configured identity.
2. Signs inside-out. Every nested Mach-O, `.xpc`, `.app`, `.appex`, `.framework`,
   `.bundle` and `.plugin` is signed deepest first with
   `codesign --force --timestamp --options runtime`, and each bundle keeps its
   entitlements. The outer app is resealed last. The action then requires
   `codesign --verify --strict --deep` to pass.
3. Signs the DMG, or zips the app with `ditto`, and runs
   `xcrun notarytool submit --wait --output-format json`. Any status other than
   `Accepted` prints `notarytool log <id>` and fails the job.
4. Runs `stapler staple` and `stapler validate` on the DMG. For a zip it staples
   the app instead and zips it again. Last, `spctl --assess` checks the result.
5. Always cleans up, including after a failure or a cancellation (SIGINT or
   SIGTERM). It restores the keychain search list, deletes the temporary
   keychain and removes the `.p12` and `.p8` files.

The keychain password and the secret inputs are masked with `::add-mask::`, and
the action never prints them in its echoed command lines.

## Usage

```yaml
jobs:
  sign-macos:
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: macos-latest
    environment: release          # secrets restricted to the default branch and v* tags
    permissions:
      contents: read
    steps:
      # ... check out the tag, build dist/MyApp.app and dist/MyApp.dmg ...
      - uses: zackees/ci.yml/actions/app-signing-macos@<40-hex sha>
        with:
          app-path: dist/MyApp.app
          artifact-path: dist/MyApp.dmg
          identity: ${{ secrets.MACOS_SIGNING_IDENTITY }}
          certificate-p12-base64: ${{ secrets.MACOS_SIGNING_CERT }}
          certificate-password: ${{ secrets.MACOS_SIGNING_CERT_PASSWORD }}
          notary-api-key-base64: ${{ secrets.MACOS_NOTARY_API_KEY }}
          notary-api-key-id: ${{ secrets.MACOS_NOTARY_API_KEY_ID }}
          notary-api-issuer-id: ${{ secrets.MACOS_NOTARY_API_ISSUER_ID }}
```

Pin the action by commit SHA. It uses no third-party actions, only Apple's
command-line tools and the `python3` that comes with the runner.

## Inputs

| Input | Required | Meaning |
| --- | --- | --- |
| `app-path` | yes | The `.app` bundle to sign. |
| `artifact-path` | no | The `.dmg` or `.zip` that ships the app. If empty, the app is zipped only for notarization and stapled in place. |
| `entitlements` | no | An entitlements plist for the outer bundle. If empty, the bundle's existing entitlements are kept. |
| `identity` | yes | `Developer ID Application: <name> (<TEAMID>)` |
| `certificate-p12-base64`, `certificate-password` | yes | The Developer ID Application `.p12`, exported with `-legacy`, and its password. |
| `notary-api-key-base64`, `notary-api-key-id`, `notary-api-issuer-id` | preferred | An App Store Connect API key (`AuthKey_<id>.p8`). |
| `notary-apple-id`, `notary-password`, `notary-team-id` | fallback | An Apple ID and an app-specific password. The team ID defaults to the `(TEAMID)` in `identity`. |
| `notarize` | no | Defaults to `true`. Set it to `false` to sign and verify without notarizing. |

Output: `submission-id`, the notarytool submission ID.
