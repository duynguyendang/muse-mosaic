package main

// Semantic evidence features.
//
// A Muse "semantic" link claims two facts are the same identifier written
// differently (a phone with a country code, an email with a typo). The engine
// must VERIFY that, not trust it. Datalog cannot do string distance, so this
// file computes mechanical, deterministic FEATURES per fact (like event_time ->
// minutes) and the policy's Datalog decides whether two features are equal:
//
//   phone_key   last 9 digits of the number, extension dropped.
//   email_dels  the local part with each single character deleted, plus the
//               address itself. Two addresses on the same domain are within
//               edit distance 1 (substitute, insert or delete) exactly when
//               they share a key (the "deletion neighbourhood" test).
//
// These are inputs, not verdicts. Nothing here marks any link verified.

import (
	"sort"
	"strings"
)

const (
	phoneKeyDigits   = 9
	emailMinLocalLen = 8  // shorter locals collide by chance, so emit no keys
	emailMaxLocalLen = 64 // bounds the key count (one key per character)
)

// phoneKey returns the last 9 digits of the phone, or "" when it has fewer.
// An extension ("x282") is dropped before digits are read.
func phoneKey(value string) string {
	if i := strings.IndexAny(value, "xX"); i >= 0 {
		value = value[:i]
	}
	digits := make([]byte, 0, len(value))
	for i := 0; i < len(value); i++ {
		if value[i] >= '0' && value[i] <= '9' {
			digits = append(digits, value[i])
		}
	}
	if len(digits) < phoneKeyDigits {
		return ""
	}
	return string(digits[len(digits)-phoneKeyDigits:])
}

// emailDeletionKeys returns the deletion neighbourhood of an email, or nil
// when the local part is too short or too long to be safe evidence.
func emailDeletionKeys(value string) []string {
	value = strings.ToLower(strings.TrimSpace(value))
	at := strings.LastIndex(value, "@")
	if at <= 0 || at == len(value)-1 {
		return nil
	}
	local, domain := value[:at], value[at:]
	if len(local) < emailMinLocalLen || len(local) > emailMaxLocalLen {
		return nil
	}
	seen := map[string]struct{}{value: {}}
	for i := 0; i < len(local); i++ {
		seen[local[:i]+local[i+1:]+domain] = struct{}{}
	}
	keys := make([]string, 0, len(seen))
	for k := range seen {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
