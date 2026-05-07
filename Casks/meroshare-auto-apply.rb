cask "meroshare-auto-apply" do
  version "2026.05.07.3"
  sha256 "e4f6afe74c995692ca7e7698164e9fd917b9182f1d21d88eac3bc2dd46bbffe9"

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

  zap trash: [
    "~/Library/Application Support/MeroShare Auto-Apply",
    "~/Library/Logs/MeroShare Auto-Apply",
    "~/Library/Preferences/com.meroshare.autoapply.plist",
    "~/Library/Saved Application State/com.meroshare.autoapply.savedState",
  ]
end
