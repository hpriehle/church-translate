// Swift's `do/catch` can't stop an Objective-C exception, and AVFAudio raises them for ordinary conditions
// such as the hardware format changing under a tap. This is the Swift target's bridging header.
#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Runs `block` and returns the exception it raised, or nil if it finished normally.
NSException * _Nullable HCCatchException(NS_NOESCAPE void (^block)(void));

NS_ASSUME_NONNULL_END
