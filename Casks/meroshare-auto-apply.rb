cask "meroshare-auto-apply" do
  version "2026.05.07.5"
  sha256 "8276cee174a047f539efb03ff3dd4040156cbbc33f15446627fb873b3bc7ea14"

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
    system_command "/usr/bin/xattr",
                   args: ["-dr", "com.apple.quarantine",
                          "#{appdir}/MeroShare Auto-Apply.app"]
  end

  zap trash: [
    "~/Library/Application Support/MeroShare Auto-Apply",
    "~/Library/Logs/MeroShare Auto-Apply",
    "~/Library/Preferences/com.meroshare.autoapply.plist",
    "~/Library/Saved Application State/com.meroshare.autoapply.savedState",
  ]
end
