package expo.modules.liveenglish

import expo.modules.kotlin.exception.CodedException
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition

// Live English isn't on Android yet (planned: NsdManager + MediaCodec Opus + Oboe). The app hides it.
class LiveEnglishModule : Module() {
  override fun definition() = ModuleDefinition {
    Name("LiveEnglish")
    Events("onSenders")
    Function("isSupported") { false }
    Function("startBrowsing") {}
    Function("stopBrowsing") {}
    AsyncFunction("play") { _: String?, _: String?, _: Int?, _: String? ->
      throw CodedException("Live English isn't available on Android yet.")
    }
    Function("stop") {}
    Function("getStats") { mapOf("active" to false, "wireless" to false, "outputName" to "") }
  }
}
