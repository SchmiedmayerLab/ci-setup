// This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
//
// SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
// SPDX-License-Identifier: MIT

import Foundation
import Darwin

// Stay alive as the responsible app while Python and its children run. exec()
// would replace this identity before macOS can present consent for the worker.
let bundle = Bundle.main
let arguments = Array(CommandLine.arguments.dropFirst())
// Apple TN3179 recommends a connected UDP socket to request Local Network
// consent before future unattended work. No send/write call is made here.
// https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy
if arguments == ["_network-consent"] {
    let deadline = Date().addingTimeInterval(120)
    repeat {
        var interfaces: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&interfaces) == 0 else { exit(1) }
        var cursor = interfaces
        var allowed = false
        while let current = cursor {
            cursor = current.pointee.ifa_next
            let flags = current.pointee.ifa_flags
            guard flags & UInt32(IFF_UP | IFF_BROADCAST) == UInt32(IFF_UP | IFF_BROADCAST),
                  let destination = current.pointee.ifa_dstaddr,
                  destination.pointee.sa_family == UInt8(AF_INET) else { continue }
            var address = destination.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee }
            address.sin_port = UInt16(9).bigEndian
            let descriptor = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
            if descriptor < 0 { continue }
            var broadcast: Int32 = 1
            _ = setsockopt(descriptor, SOL_SOCKET, SO_BROADCAST, &broadcast, socklen_t(MemoryLayout<Int32>.size))
            let code = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
            close(descriptor)
            if code == 0 { allowed = true }
        }
        if let interfaces { freeifaddrs(interfaces) }
        if allowed { exit(0) }
        Thread.sleep(forTimeInterval: 1)
    } while Date() < deadline
    fputs("Local Network access could not be established. Check Runner Host in Privacy & Security.\n", stderr)
    exit(1)
}
guard let python = bundle.object(forInfoDictionaryKey: "RunnerPython") as? String,
      let root = bundle.object(forInfoDictionaryKey: "RunnerRoot") as? String,
      python.hasPrefix("/"), root.hasPrefix("/"),
      let command = arguments.first,
      ["host", "install", "_host-probe"].contains(command) else {
    fputs("Invalid runner-host invocation or application configuration.\n", stderr)
    exit(64)
}
let worker = Process()
worker.executableURL = URL(fileURLWithPath: python)
worker.arguments = [root + "/bin/ci-setup"] + arguments
worker.currentDirectoryURL = URL(fileURLWithPath: root)
worker.standardInput = FileHandle.standardInput
worker.standardOutput = FileHandle.standardOutput
worker.standardError = FileHandle.standardError
var sources: [DispatchSourceSignal] = []
for number in [SIGTERM, SIGINT, SIGHUP] {
    signal(number, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: number, queue: .global())
    source.setEventHandler {
        if worker.isRunning { kill(worker.processIdentifier, number) }
    }
    source.resume()
    sources.append(source)
}
do {
    try worker.run()
    worker.waitUntilExit()
    exit(worker.terminationReason == .exit ? worker.terminationStatus : 128 + worker.terminationStatus)
} catch {
    // Never echo child arguments, environment, or authentication output.
    fputs("Could not start the prepared runner-host interpreter.\n", stderr)
    exit(1)
}
