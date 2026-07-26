# Lumen Native Apps

Lumen now ships from one Capacitor project to:

- iPhone and iPad
- Mac through Mac Catalyst
- Android phones and tablets through Google Play

The native apps securely display the production Flask application at
`https://lumenboard.org/app`. Camera capture stays on-device until the existing
Lumen APIs receive a user-requested analysis. Authentication opens in the
system browser and returns through the `org.lumenboard.app://auth/callback`
deep link.

## Project locations

- Apple workspace: `ios/App/App.xcworkspace`
- Android project: `android/`
- Shared native configuration: `capacitor.config.ts`
- Store icon: `store-assets/lumen-app-icon.png`

The application ID/bundle ID is `org.lumenboard.app`.

## Required one-time configuration

1. In Supabase Dashboard, open **Authentication → URL Configuration** and add
   this exact redirect URL:

   `org.lumenboard.app://auth/callback`

2. Keep `https://lumenboard.org/auth/callback` as an allowed redirect for the
   web application.
3. Confirm `https://lumenboard.org` has a valid TLS certificate and production
   Supabase/Anthropic environment variables.
4. Prepare public Privacy Policy and Support URLs for both stores.

## Build the Apple apps

Install the full Xcode application, open `ios/App/App.xcworkspace`, and:

1. Select the **App** target.
2. Choose your Apple Developer team under Signing & Capabilities.
3. Keep bundle ID `org.lumenboard.app`.
4. For iPhone/iPad, select **Any iOS Device**, then **Product → Archive**.
5. For Mac, select **My Mac (Mac Catalyst)**, then **Product → Archive**.
6. Validate and upload each archive from Xcode Organizer.

The project supports iPhone, iPad, portrait/landscape rotation, camera access,
photo-library access, and Mac Catalyst.

## Direct-download Mac app

The Mac app can also be built without the Mac App Store:

```bash
bash scripts/build_macos_app.sh
```

This creates `dist/macos/Lumen.app`, `dist/macos/Lumen-macOS.zip`, and the
website download at `static/downloads/Lumen-macOS.zip`. The current
command-line build supports Apple Silicon Macs and is ad-hoc signed, so a user
must Control-click the app, choose **Open**, and confirm the first launch. The
full Xcode toolchain is needed to add Intel support. A paid Apple Developer
membership is still required later for Developer ID signing and notarization,
which removes the Gatekeeper friction.

## Build the Android app

Install Android Studio with Android SDK 35 and a current JDK. Open `android/`,
allow Gradle to sync, then:

1. Test on a physical device (camera behavior cannot be fully tested on every
   emulator).
2. Choose **Build → Generate Signed Bundle / APK**.
3. Select **Android App Bundle**.
4. Create and securely back up a release keystore.
5. Upload the generated `.aab` to Google Play Console.

The Android package is `org.lumenboard.app`, targets SDK 35, and has internet
and camera permissions.

## Day-to-day native workflow

After changing dependencies or `capacitor.config.ts`:

```bash
npm install
npm run sync
```

Open a platform project:

```bash
npm run open:ios
npm run open:android
```

To point a development build at another deployed backend:

```bash
LUMEN_APP_URL=https://staging.example.com/app npm run sync
```

Do not use a LAN or cleartext URL for release builds.

## Store submission checklist

- Apple Developer Program and Google Play Console accounts
- App name, subtitle/short description, full description, category, and age rating
- iPhone, iPad, Mac, Android phone, and Android tablet screenshots
- Privacy Policy URL and support contact
- App privacy/data safety disclosures for account data, photos/camera, and AI requests
- Review account or clear Google-login instructions for store reviewers
- Signed Apple archives and Android App Bundle
- Physical-device checks for sign-in, camera permission, capture, analysis,
  history, logout, offline screen, and account deletion/support flow

## Important architecture note

The Flask/Anthropic backend is not bundled into the phone. This is intentional:
API keys remain server-side, updates to Lumen's analysis experience deploy
without resubmitting every store binary, and all platforms share the same user
history.
