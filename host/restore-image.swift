// This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
//
// SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
// SPDX-License-Identifier: MIT

import Foundation
import Virtualization

func describe(_ result: Result<VZMacOSRestoreImage, Error>) {
    do {
        let image = try result.get()
        guard let requirements = image.mostFeaturefulSupportedConfiguration,
              requirements.hardwareModel.isSupported else {
            fputs("Restore image is not supported by this Mac.\n", stderr)
            exit(1)
        }
        let version = image.operatingSystemVersion
        let data: [String: Any] = [
            "url": image.url.absoluteString,
            "build": image.buildVersion,
            "version": "\(version.majorVersion).\(version.minorVersion).\(version.patchVersion)",
            "minimum_cpus": requirements.minimumSupportedCPUCount,
            "minimum_memory_bytes": requirements.minimumSupportedMemorySize
        ]
        let json = try JSONSerialization.data(withJSONObject: data, options: [.sortedKeys])
        print(String(decoding: json, as: UTF8.self))
        exit(0)
    } catch {
        let failure = error as NSError
        fputs("Apple restore-image service failed (\(failure.domain), code \(failure.code)); retry or use --image with a verified local IPSW.\n", stderr)
        exit(1)
    }
}

if CommandLine.arguments.count == 1 {
    VZMacOSRestoreImage.fetchLatestSupported(completionHandler: describe)
} else if CommandLine.arguments.count == 2 {
    VZMacOSRestoreImage.load(from: URL(fileURLWithPath: CommandLine.arguments[1]), completionHandler: describe)
} else {
    fputs("Usage: restore-image.swift [local.ipsw]\n", stderr)
    exit(2)
}
RunLoop.main.run()
