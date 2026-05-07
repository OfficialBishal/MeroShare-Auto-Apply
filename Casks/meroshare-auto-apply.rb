cask "meroshare-auto-apply" do
  version "2026.05.07.8"
  sha256 "3b327159e6419b3cfc8895d626b44ca16e36f0dffcab978da36dd679eae53860"

  url "https://github.com/OfficialBishal/MeroShare-Auto-Apply/releases/download/v#{version}/MeroShare-Auto-Apply.dmg"
  name "MeroShare Auto-Apply"
  desc "Menu bar tool that auto-applies to NEPSE IPOs and rights via MeroShare"
  homepage "https://github.com/OfficialBishal/MeroShare-Auto-Apply"

  livecheck do
    url :url
    strategy :github_latest
  end

  auto_updates true
  depends_on macos: ">= :catalina"

  app "MeroShare Auto-Apply.app"

  # Strip the macOS quarantine flag after install. The bundle is
  # ad-hoc signed (no $99 Apple Developer ID), and macOS Sequoia
  # rejects ad-hoc-signed apps that came through the quarantine
  # flag with a "damaged and can't be opened" dialog — even when
  # the signature is valid. The cask install path already verified
  # the .dmg's SHA-256 against the formula, so stripping here just
  # saves users from running `xattr -dr com.apple.quarantine`
  # themselves after every install / upgrade.
  postflight do
    # Strip quarantine: macOS Sequoia rejects ad-hoc-signed bundles
    # downloaded through the quarantine flag with a "damaged" dialog
    # even when the signature is valid.
    system_command "/usr/bin/xattr",
                   args: ["-dr", "com.apple.quarantine",
                          "#{appdir}/MeroShare Auto-Apply.app"]
    # Force-refresh the Launch Services cache for this bundle so
    # subsequent `open`/Finder launches pick up the freshly-installed
    # version. After a `brew upgrade` the old bundle's record can
    # linger and intercept launches, leaving the user staring at a
    # menu bar that never appears.
    system_command "/System/Library/Frameworks/CoreServices.framework/" \
                   "Versions/A/Frameworks/LaunchServices.framework/Versions/A/" \
                   "Support/lsregister",
                   args: ["-f", "#{appdir}/MeroShare Auto-Apply.app"]
  end

  zap trash: [
    "~/Library/Application Support/MeroShare Auto-Apply",
    "~/Library/Logs/MeroShare Auto-Apply",
    "~/Library/Preferences/com.meroshare.autoapply.plist",
    "~/Library/Saved Application State/com.meroshare.autoapply.savedState",
  ]
end
