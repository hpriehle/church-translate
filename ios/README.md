# Hear Church: Live English (iPhone)

Plays the office sender's stream (`live/sender.py`) with as little delay as the phone allows:
UDP via Network.framework, libopus (alta/swift-opus), an adaptive jitter buffer sized from the
measured Wi-Fi timing wobble (10–60 ms), and AVAudioEngine with a 5 ms IO buffer.
Finds the sender over Bonjour (`_hearchurch-live._udp`), or by an address typed under Details.

Build on the Mac:

```
cd ios && xcodegen
xcodebuild test -project HearLive.xcodeproj -scheme HearLive -destination 'platform=iOS Simulator,name=<sim>'
```

Launch arguments for testing: `-autoplay` starts listening at launch, `-details` opens the readout.
On a real phone, open the project in Xcode and run it (team 5473W6M839). The first launch asks
for Local Network access, which it needs.
