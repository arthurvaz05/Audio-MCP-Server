// tap-capture <out.wav>: record a meeting as mono 16-bit WAV = mix of the default
// mic + all system audio (Core Audio global tap). Requires macOS 14.4+ and
// "System Audio Recording" permission for the responsible app.
//
// The helper records itself because a tap read by ANOTHER process (e.g. Python
// through a public aggregate) delivers silence (measured 2026-11-07). Tap and
// aggregate are private, so they die with this process: nothing is left behind.
//
// Global, not per-process: a per-process tap created on an app whose audio kept
// playing across a previous tap's teardown gets ZERO IO callbacks (measured
// 2026-11-08: 1st recording ok, every next one silent until Chrome's stream
// restarted). A global tap has no such state; it records what BlackHole did.
//
// Stdout protocol (one flushed line each):
//   READY tap_channels=<count> rate=<hz>       audio is actually flowing
//   DONE seconds=<s> tap_peak=<p>              after SIGTERM/SIGINT/stdin EOF, WAV finalized
//   ERROR <message>                            then exit 1 (caller falls back)
import AudioToolbox
import CoreAudio
import Foundation
import Synchronization

let firstAudioTimeout: TimeInterval = 4  // no IO callback by then → ERROR, caller falls back

func say(_ line: String) {
    print(line)
    fflush(stdout)
}

func fail(_ message: String) -> Never {
    say("ERROR \(message)")
    exit(1)
}

func address(_ selector: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func stringProperty(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = address(selector)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr,
          let cf = value?.takeRetainedValue() else { return nil }
    return cf as String
}

func inputChannelCount(_ device: AudioObjectID) -> Int {
    var addr = address(kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeInput)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(device, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(device, &addr, 0, nil, &size, raw) == noErr else { return 0 }
    let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return list.reduce(0) { $0 + Int($1.mNumberChannels) }
}

// ---- setup ----

guard CommandLine.arguments.count == 2 else { fail("usage: tap-capture <out.wav>") }
let outURL = URL(fileURLWithPath: CommandLine.arguments[1])

let tapDescription = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tapDescription.uuid = UUID()
tapDescription.muteBehavior = .unmuted   // user keeps hearing the call
tapDescription.isPrivate = true
var tapID = AudioObjectID(kAudioObjectUnknown)
if AudioHardwareCreateProcessTap(tapDescription, &tapID) != noErr { fail("AudioHardwareCreateProcessTap failed") }

var micID = AudioObjectID(kAudioObjectUnknown)
var micAddr = address(kAudioHardwarePropertyDefaultInputDevice)
var micSize = UInt32(MemoryLayout<AudioObjectID>.size)
AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &micAddr, 0, nil, &micSize, &micID)
guard let micUID = stringProperty(micID, kAudioDevicePropertyDeviceUID) else {
    AudioHardwareDestroyProcessTap(tapID)
    fail("no default input device")
}
let micChannels = inputChannelCount(micID)

let aggregate: [String: Any] = [
    kAudioAggregateDeviceNameKey: "Gravar Reunião (Tap)",
    kAudioAggregateDeviceUIDKey: "com.monkai.gravar-reuniao-tap.\(UUID().uuidString)",
    kAudioAggregateDeviceMainSubDeviceKey: micUID,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: micUID]],
    kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: tapDescription.uuid.uuidString,
                                       kAudioSubTapDriftCompensationKey: true]],
]
var aggregateID = AudioObjectID(kAudioObjectUnknown)
if AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID) != noErr {
    AudioHardwareDestroyProcessTap(tapID)
    fail("AudioHardwareCreateAggregateDevice failed")
}

var rate = Float64(0)
var rateAddr = address(kAudioDevicePropertyNominalSampleRate)
var rateSize = UInt32(MemoryLayout<Float64>.size)
AudioObjectGetPropertyData(aggregateID, &rateAddr, 0, nil, &rateSize, &rate)

// Output file: mono Int16 WAV at the aggregate's rate; client side is mono Float32.
var fileFormat = AudioStreamBasicDescription(
    mSampleRate: rate, mFormatID: kAudioFormatLinearPCM,
    mFormatFlags: kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked,
    mBytesPerPacket: 2, mFramesPerPacket: 1, mBytesPerFrame: 2, mChannelsPerFrame: 1,
    mBitsPerChannel: 16, mReserved: 0)
var clientFormat = AudioStreamBasicDescription(
    mSampleRate: rate, mFormatID: kAudioFormatLinearPCM,
    mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
    mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4, mChannelsPerFrame: 1,
    mBitsPerChannel: 32, mReserved: 0)
var fileRef: ExtAudioFileRef?
if ExtAudioFileCreateWithURL(outURL as CFURL, kAudioFileWAVEType, &fileFormat, nil,
                             AudioFileFlags.eraseFile.rawValue, &fileRef) != noErr {
    AudioHardwareDestroyAggregateDevice(aggregateID)
    AudioHardwareDestroyProcessTap(tapID)
    fail("cannot create \(outURL.path)")
}
let file = fileRef!
ExtAudioFileSetProperty(file, kExtAudioFileProperty_ClientDataFormat,
                        UInt32(MemoryLayout<AudioStreamBasicDescription>.size), &clientFormat)
ExtAudioFileWriteAsync(file, 0, nil)  // primes async writer (realtime-safe after this)

// Realtime state, touched only by the IOProc until it is stopped.
let maxFrames = 16384
let mono = UnsafeMutablePointer<Float>.allocate(capacity: maxFrames)
let framesWritten = Atomic<Int64>(0)  // written on the IO thread, read on main
var tapPeak: Float = 0

var procID: AudioDeviceIOProcID?
let ioStatus = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, nil) { _, inData, _, _, _ in
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
    var frames = 0
    for buf in buffers where buf.mNumberChannels > 0 {
        frames = max(frames, Int(buf.mDataByteSize) / (4 * Int(buf.mNumberChannels)))
    }
    frames = min(frames, maxFrames)
    guard frames > 0 else { return }
    mono.update(repeating: 0, count: frames)
    var channel = 0
    var totalChannels = 0
    for buf in buffers {
        let n = Int(buf.mNumberChannels)
        guard n > 0, let data = buf.mData?.assumingMemoryBound(to: Float.self) else { channel += n; continue }
        let bufFrames = min(frames, Int(buf.mDataByteSize) / (4 * n))
        for c in 0..<n {
            let isTap = channel + c >= micChannels  // aggregate: mic channels first, then tap
            for f in 0..<bufFrames {
                let s = data[f * n + c]
                mono[f] += s
                if isTap { tapPeak = max(tapPeak, abs(s)) }
            }
        }
        channel += n
        totalChannels += n
    }
    if totalChannels > 0 {
        let scale = 1 / Float(totalChannels)
        for f in 0..<frames { mono[f] = min(1, max(-1, mono[f] * scale)) }
    }
    var abl = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(
        mNumberChannels: 1, mDataByteSize: UInt32(frames * 4), mData: mono))
    ExtAudioFileWriteAsync(file, UInt32(frames), &abl)
    framesWritten.add(Int64(frames), ordering: .relaxed)
}
if ioStatus != noErr || AudioDeviceStart(aggregateID, procID) != noErr {
    ExtAudioFileDispose(file)
    AudioHardwareDestroyAggregateDevice(aggregateID)
    AudioHardwareDestroyProcessTap(tapID)
    fail("cannot start capture IO")
}
// AudioDeviceStart can succeed with no IO ever delivered: only claim READY once
// frames flow, so the caller falls back instead of recording a silent file.
let deadline = Date().addingTimeInterval(firstAudioTimeout)
while framesWritten.load(ordering: .relaxed) == 0 && Date() < deadline { usleep(50_000) }
if framesWritten.load(ordering: .relaxed) == 0 {
    AudioDeviceStop(aggregateID, procID)
    ExtAudioFileDispose(file)
    AudioHardwareDestroyAggregateDevice(aggregateID)
    AudioHardwareDestroyProcessTap(tapID)
    fail("no audio IO within \(Int(firstAudioTimeout))s")
}
say("READY tap_channels=\(max(0, inputChannelCount(aggregateID) - micChannels)) rate=\(Int(rate))")

func finishAndExit() -> Never {
    AudioDeviceStop(aggregateID, procID)
    if let procID { AudioDeviceDestroyIOProcID(aggregateID, procID) }
    ExtAudioFileDispose(file)  // flushes async writes, finalizes WAV header
    AudioHardwareDestroyAggregateDevice(aggregateID)
    AudioHardwareDestroyProcessTap(tapID)
    say(String(format: "DONE seconds=%.1f tap_peak=%.4f", Double(framesWritten.load(ordering: .relaxed)) / rate, tapPeak))
    exit(0)
}

// ---- lifetime ----

signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let sources = [SIGTERM, SIGINT].map { sig -> DispatchSourceSignal in
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { finishAndExit() }
    src.resume()
    return src
}
// Parent died (pipe closed) → stdin EOF → finalize the WAV anyway.
DispatchQueue.global().async {
    while FileHandle.standardInput.availableData.count > 0 {}
    DispatchQueue.main.async { finishAndExit() }
}
withExtendedLifetime(sources) { dispatchMain() }
