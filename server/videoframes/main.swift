// Evenly spaced frames from a video, and what it is, for an AI that can look at pictures but not play video.
// usage: videoframes INPUT OUTDIR COUNT. Prints JSON: duration, width, height, codec, audio, frames.
import AVFoundation
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

let args = CommandLine.arguments
guard args.count == 4, let count = Int(args[3]), count > 0 else {
    FileHandle.standardError.write(Data("usage: videoframes INPUT OUTDIR COUNT\n".utf8)); exit(2)
}
let asset = AVURLAsset(url: URL(fileURLWithPath: args[1]))

func fail(_ why: String) -> Never { FileHandle.standardError.write(Data((why + "\n").utf8)); exit(1) }

func describe() async throws -> [String: Any] {
    let duration = try await asset.load(.duration).seconds
    guard let track = try await asset.loadTracks(withMediaType: .video).first else { fail("no video track") }
    let size = try await track.load(.naturalSize).applying(try await track.load(.preferredTransform))
    let formats = try await track.load(.formatDescriptions)
    let codec = formats.first.map { d -> String in
        let c = CMFormatDescriptionGetMediaSubType(d).bigEndian
        return withUnsafeBytes(of: c) { String(decoding: $0, as: UTF8.self) }
    } ?? ""
    let audio = try await asset.loadTracks(withMediaType: .audio).isEmpty == false

    let generator = AVAssetImageGenerator(asset: asset)
    generator.appliesPreferredTrackTransform = true
    generator.maximumSize = CGSize(width: 1280, height: 1280)
    generator.requestedTimeToleranceBefore = CMTime(seconds: 0.5, preferredTimescale: 600)
    generator.requestedTimeToleranceAfter = CMTime(seconds: 0.5, preferredTimescale: 600)
    var frames: [[String: Any]] = []
    for i in 0..<count {
        let second = duration * (Double(i) + 0.5) / Double(count)
        guard let (image, _) = try? await generator.image(at: CMTime(seconds: second, preferredTimescale: 600)) else { continue }
        let out = URL(fileURLWithPath: args[2]).appendingPathComponent(String(format: "frame_%02d.jpg", i + 1))
        guard let dest = CGImageDestinationCreateWithURL(out as CFURL, UTType.jpeg.identifier as CFString, 1, nil) else { continue }
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: 0.8] as CFDictionary)
        if CGImageDestinationFinalize(dest) { frames.append(["second": second, "path": out.path]) }
    }
    return ["duration": duration, "width": abs(Int(size.width)), "height": abs(Int(size.height)),
            "codec": codec, "audio": audio, "frames": frames]
}

let done = DispatchSemaphore(value: 0)
Task {
    do {
        let info = try await describe()
        let data = try JSONSerialization.data(withJSONObject: info)
        FileHandle.standardOutput.write(data)
    } catch { fail("\(error.localizedDescription)") }
    done.signal()
}
done.wait()
