// Writes everything the Mac is playing to stdout, using ScreenCaptureKit, so no output device has
// to be changed. Each buffer is written as a record:
//   float64 presentation time (host clock, seconds; same clock as time.monotonic() and the mic's
//   hardware timestamps), uint32 sample count, then float32 mono 16 kHz samples.
// Needs Screen Recording permission.
//
//   swiftc -O -o syscap main.swift
import AVFoundation
import ScreenCaptureKit

let rate = 16000
let out = FileHandle.standardOutput

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

final class Sink: NSObject, SCStreamOutput, SCStreamDelegate {
    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid, let fmt = sb.formatDescription?.audioStreamBasicDescription
        else { return }
        let samples = mono(sb, channels: Int(fmt.mChannelsPerFrame))
        guard !samples.isEmpty else { return }
        var pts = sb.presentationTimeStamp.seconds
        var n = UInt32(samples.count)
        var rec = Data(bytes: &pts, count: 8)
        rec.append(Data(bytes: &n, count: 4))
        samples.withUnsafeBufferPointer { rec.append(Data(buffer: $0)) }
        out.write(rec)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fail("capture stopped: \(error.localizedDescription)")
    }

    func mono(_ sb: CMSampleBuffer, channels: Int) -> [Float] {
        var result = [Float]()
        try? sb.withAudioBufferList { list, _ in
            let bufs = Array(list)
            if bufs.count == 1 {  // interleaved (or mono)
                let p = bufs[0].mData!.assumingMemoryBound(to: Float.self)
                let n = Int(bufs[0].mDataByteSize) / 4 / channels
                result = (0..<n).map { i in (0..<channels).reduce(0) { $0 + p[i * channels + $1] } / Float(channels) }
            } else {  // one buffer per channel
                let n = Int(bufs[0].mDataByteSize) / 4
                result = [Float](repeating: 0, count: n)
                for b in bufs {
                    let p = b.mData!.assumingMemoryBound(to: Float.self)
                    for i in 0..<n { result[i] += p[i] / Float(bufs.count) }
                }
            }
        }
        return result
    }
}

let sink = Sink()
var stream: SCStream?  // must stay referenced, or capture silently stops
Task {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else { fail("no display") }
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.sampleRate = rate
        cfg.channelCount = 1
        cfg.excludesCurrentProcessAudio = true
        cfg.width = 2  // video is required but unused: keep it tiny and slow
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        let s = SCStream(filter: SCContentFilter(display: display, excludingWindows: []),
                         configuration: cfg, delegate: sink)
        let q = DispatchQueue(label: "audio")
        try s.addStreamOutput(sink, type: .audio, sampleHandlerQueue: q)
        try s.addStreamOutput(sink, type: .screen, sampleHandlerQueue: q)
        try await s.startCapture()
        stream = s
        FileHandle.standardError.write("capturing\n".data(using: .utf8)!)
    } catch {
        fail("cannot capture system audio: \(error.localizedDescription) (Screen Recording permission?)")
    }
}
signal(SIGPIPE) { _ in exit(0) }  // server closed the pipe
dispatchMain()
