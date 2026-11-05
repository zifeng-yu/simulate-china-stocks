import Foundation

struct StatusReason: Codable {
    let code: String
    let message: String
}

struct AccountReadiness: Codable {
    let account_id: String
    let ready: Bool
    let summary: String
    let reasons: [StatusReason]
}

struct ReadinessPayload: Codable {
    let schema_version: Int
    let state: String
    let label: String
    let business_session: String?
    let target_session: String?
    let sampled_at: String
    let expires_at: String
    let accounts: [AccountReadiness]
    let reasons: [StatusReason]
    let optional_work: [String]
}

enum StatusModel {
    static let maximumBytes = 256 * 1024
    static let states: Set<String> = ["READY", "WORKING", "WAITING", "BLOCKED", "UNKNOWN"]
    static let accounts: Set<String> = ["S1", "S1-LT", "LLM"]

    static func timestamp(_ value: String) -> Date? {
        let format = ISO8601DateFormatter()
        format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let result = format.date(from: value) { return result }
        format.formatOptions = [.withInternetDateTime]
        return format.date(from: value)
    }

    static func issue(_ value: ReadinessPayload, now: Date) -> String? {
        guard value.schema_version == 1, states.contains(value.state) else { return "INVALID_SCHEMA" }
        guard let sampled = timestamp(value.sampled_at), let expires = timestamp(value.expires_at),
              sampled <= now, expires > sampled, expires.timeIntervalSince(sampled) <= 90.01
        else { return "INVALID_CLOCK" }
        guard now < expires else { return "EXPIRED" }
        guard value.accounts.count <= 3, Set(value.accounts.map { $0.account_id }).count == value.accounts.count,
              value.accounts.allSatisfy({ accounts.contains($0.account_id) }) else { return "INVALID_ACCOUNTS" }
        if value.state == "READY" {
            guard Set(value.accounts.map { $0.account_id }) == accounts,
                  value.accounts.allSatisfy({ $0.ready }),
                  validDay(value.business_session), validDay(value.target_session),
                  value.business_session! < value.target_session!
            else { return "INCOMPLETE_READY" }
            var calendar = Calendar(identifier: .gregorian)
            calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
            let day = calendar.startOfDay(for: sampled)
            let tomorrow = calendar.date(byAdding: .day, value: 1, to: day)!
            let format = DateFormatter(); format.timeZone = calendar.timeZone
            format.locale = Locale(identifier: "en_US_POSIX"); format.dateFormat = "yyyy-MM-dd"
            let today = format.string(from: sampled)
            var boundaries = [tomorrow]
            // D and N are the backend's verified trading days. Intervening holiday/weekend
            // dates have no trading-day cutoff; their lease still ends at midnight.
            if today == value.business_session || today == value.target_session {
                boundaries += [calendar.date(bySettingHour: 9, minute: 0, second: 0, of: day)!,
                    calendar.date(bySettingHour: 18, minute: 10, second: 0, of: day)!]
            }
            guard expires <= boundaries.filter({ $0 > sampled }).min()! else {
                return "CROSSES_DAY_BOUNDARY"
            }
            guard value.business_session! <= today, value.target_session! >= today,
                  value.target_session! != today || calendar.component(.hour, from: sampled) < 9
            else { return "INVALID_TARGET_DAY" }
        }
        return nil
    }

    static func decode(_ data: Data, now: Date) throws -> ReadinessPayload {
        guard data.count <= maximumBytes else { throw ProbeFailure("OUTPUT_LIMIT") }
        let value: ReadinessPayload
        do { value = try JSONDecoder().decode(ReadinessPayload.self, from: data) }
        catch { throw ProbeFailure("INVALID_JSON") }
        if let code = issue(value, now: now) { throw ProbeFailure(code) }
        return value
    }

    static func validDay(_ value: String?) -> Bool {
        guard let value = value, value.count == 10 else { return false }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.isLenient = false
        return formatter.date(from: value).map { formatter.string(from: $0) == value } ?? false
    }

    static func label(state: String, payload: ReadinessPayload?) -> String {
        switch state {
        case "READY": return "可关机"
        case "WORKING": return "业务处理中"
        case "WAITING": return "尚未就绪"
        case "BLOCKED": return "需要处理"
        default:
            return payload?.state == "UNKNOWN" && payload?.label == "待晚间" ? "待晚间" : "待核查"
        }
    }
}

struct ProbeFailure: Error {
    let code: String
    let diagnostic: [String]
    init(_ code: String, diagnostic: [String] = []) { self.code = code; self.diagnostic = diagnostic }
}

struct ProbeGate {
    private(set) var generation = 0
    private(set) var sleeping = false
    private(set) var inFlight = false
    mutating func invalidate() { generation += 1 }
    mutating func sleep() { sleeping = true; invalidate() }
    mutating func wake() { sleeping = false; invalidate() }
    mutating func begin() -> Int? {
        guard !sleeping && !inFlight else { return nil }
        inFlight = true
        return generation
    }
    mutating func complete(_ token: Int) -> Bool {
        inFlight = false
        return !sleeping && token == generation
    }
}
