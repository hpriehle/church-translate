Pod::Spec.new do |s|
  s.name           = 'LiveEnglish'
  s.version        = '0.1.0'
  s.summary        = 'Low-delay room audio over Wi-Fi'
  s.description    = 'Receives the Hear Church Live English UDP stream (Opus 5 ms) and plays it with minimal delay.'
  s.author         = 'Hear Church'
  s.homepage       = 'https://hearchurch.com'
  s.license        = 'MIT'
  s.platforms      = { :ios => '16.4' }
  s.source         = { git: '' }
  s.static_framework = true

  s.dependency 'ExpoModulesCore'

  # libopus 1.4 (BSD, opus/COPYING), compiled in: float build, decoder used.
  s.source_files = '*.swift', 'opus/include/*.h', 'opus/celt/*.c', 'opus/silk/*.c', 'opus/silk/float/*.c', 'opus/src/*.c'
  s.public_header_files = 'opus/include/*.h'
  s.preserve_paths = 'opus/**/*.h', 'opus/COPYING'
  s.exclude_files = 'opus/src/opus_demo.c', 'opus/src/opus_compare.c', 'opus/src/repacketizer_demo.c'
  # libopus is plain C: keep it out of Clang modules so it doesn't scan React Native's C++ headers.
  s.compiler_flags = '-fno-modules'
  s.pod_target_xcconfig = {
    'DEFINES_MODULE' => 'YES',
    # Quoted includes (-iquote) are searched before React Native's paths: libopus's debug.h would
    # otherwise resolve to React/Debug.h on the case-insensitive Mac filesystem.
    'USER_HEADER_SEARCH_PATHS' => '"$(PODS_TARGET_SRCROOT)/opus" "$(PODS_TARGET_SRCROOT)/opus/include" "$(PODS_TARGET_SRCROOT)/opus/celt" "$(PODS_TARGET_SRCROOT)/opus/silk" "$(PODS_TARGET_SRCROOT)/opus/silk/float"',
    'GCC_PREPROCESSOR_DEFINITIONS' => '$(inherited) OPUS_BUILD=1 VAR_ARRAYS=1 FLOATING_POINT=1 HAVE_LRINT=1 HAVE_LRINTF=1 HAVE_STDINT_H=1 HAVE_STDLIB_H=1 HAVE_STRING_H=1 HAVE_INTTYPES_H=1',
    'GCC_WARN_INHIBIT_ALL_WARNINGS' => 'YES',
    'CLANG_ENABLE_EXPLICIT_MODULES' => 'NO',
    'USE_HEADERMAP' => 'NO',
  }
end
