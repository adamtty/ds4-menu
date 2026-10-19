// Turns a black-on-white logo into menu bar template PNGs (alpha = darkness), cropped to the ink.
// Usage: swift make-icon.swift logo.png outdir/   → outdir/logo.png (1x) and outdir/logo@2x.png
import AppKit

let args = CommandLine.arguments
guard args.count == 3, let src = NSImage(contentsOfFile: args[1]),
      let cg = src.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    print("usage: swift make-icon.swift logo.png outdir/"); exit(1)
}
let w = cg.width, h = cg.height
var px = [UInt8](repeating: 0, count: w * h * 4)
let ctx = CGContext(data: &px, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                    space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))

// ink = how dark a pixel is (transparent counts as paper)
func ink(_ i: Int) -> UInt8 {
    let a = Int(px[i * 4 + 3]), lum = (Int(px[i * 4]) + Int(px[i * 4 + 1]) + Int(px[i * 4 + 2])) / 3
    return UInt8((255 - (lum * a + 255 * (255 - a)) / 255))
}
var minX = w, minY = h, maxX = 0, maxY = 0
for y in 0..<h { for x in 0..<w where ink(y * w + x) > 40 {
    minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y)
} }
let cw = maxX - minX + 1, ch = maxY - minY + 1
var mask = [UInt8](repeating: 0, count: cw * ch * 4)
for y in 0..<ch { for x in 0..<cw {
    let o = (y * cw + x) * 4
    mask[o + 3] = ink((y + minY) * w + x + minX)  // black, alpha = ink (premultiplied, so rgb stays 0)
} }
let cropped = CGContext(data: &mask, width: cw, height: ch, bitsPerComponent: 8, bytesPerRow: cw * 4,
                        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!.makeImage()!

let pointHeight = 13.0  // reads like the other menu bar icons
for (scale, suffix) in [(1.0, ""), (2.0, "@2x")] {
    let oh = Int(pointHeight * scale), ow = Int((Double(cw) / Double(ch) * pointHeight * scale).rounded())
    let out = CGContext(data: nil, width: ow, height: oh, bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    out.interpolationQuality = .high
    out.draw(cropped, in: CGRect(x: 0, y: 0, width: ow, height: oh))
    let rep = NSBitmapImageRep(cgImage: out.makeImage()!)
    let path = (args[2] as NSString).appendingPathComponent("logo\(suffix).png")
    try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: path))
    print("\(path): \(ow)x\(oh)")
}
