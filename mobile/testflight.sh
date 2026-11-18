#!/bin/bash
# Build Hear Church for the App Store and upload it to TestFlight.
# Run in Terminal on the Mac (signing needs the login keychain and Xcode's Apple account).
# Bump expo.ios.buildNumber in app.json before each upload.
set -euo pipefail
cd "$(dirname "$0")"
export LANG=en_US.UTF-8
npm install --silent
npx expo prebuild --platform ios --clean
cd ios
rm -rf build/HearChurch.xcarchive build/ipa
xcodebuild archive -workspace HearChurch.xcworkspace -scheme HearChurch -configuration Release \
  -destination 'generic/platform=iOS' -archivePath build/HearChurch.xcarchive \
  -allowProvisioningUpdates -quiet
cat > build/ExportOptions.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>app-store-connect</string>
  <key>teamID</key><string>5473W6M839</string>
  <key>destination</key><string>upload</string>
  <key>signingStyle</key><string>automatic</string>
  <key>uploadSymbols</key><true/>
  <key>manageAppVersionAndBuildNumber</key><false/>
</dict></plist>
PLIST
xcodebuild -exportArchive -archivePath build/HearChurch.xcarchive \
  -exportOptionsPlist build/ExportOptions.plist -exportPath build/ipa -allowProvisioningUpdates
echo
echo "Uploaded. It shows in App Store Connect > TestFlight in 5-15 minutes."
