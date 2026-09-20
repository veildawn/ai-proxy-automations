package main

// Signature verification for every bundle this marketplace publishes.
//
// The gateway verifies a manifest against m.Canonical(): the typed manifest
// marshalled, then re-marshalled generically so object keys are sorted. The
// marketplace signer (ai-automation-source/scripts/sign_manifest.go) marshals
// the same document generically with signature removed, which produces the same
// bytes only while the manifest carries nothing the gateway's struct would drop
// or re-encode. Verifying here, in Go, against the same encoder, is what keeps
// that agreement from rotting silently: a republish that touches one field and
// forgets to re-sign fails CI instead of failing on a user's machine.
//
// Structural rules (runtime whitelist, job/module binding, exports) live in
// scripts/validate.py; `make check` runs both.

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

type publisher struct {
	Handle string `json:"handle"`
	Keys   []struct {
		KeyID     string `json:"key_id"`
		PublicKey string `json:"public_key"`
	} `json:"keys"`
}

type revocation struct {
	Revoked []struct {
		KeyID string `json:"key_id"`
	} `json:"revoked"`
}

func main() {
	revoked := map[string]bool{}
	if raw, err := os.ReadFile("revoke.json"); err == nil {
		var doc revocation
		if err := json.Unmarshal(raw, &doc); err != nil {
			panic(err)
		}
		for _, item := range doc.Revoked {
			revoked[item.KeyID] = true
		}
	}
	keys := map[string]map[string]ed25519.PublicKey{}
	publisherPaths, _ := filepath.Glob("publishers/*.json")
	for _, path := range publisherPaths {
		raw, err := os.ReadFile(path)
		if err != nil {
			panic(err)
		}
		var pub publisher
		if err := json.Unmarshal(raw, &pub); err != nil {
			panic(err)
		}
		keys[pub.Handle] = map[string]ed25519.PublicKey{}
		for _, item := range pub.Keys {
			key, err := base64.StdEncoding.DecodeString(item.PublicKey)
			if err != nil {
				panic(err)
			}
			keys[pub.Handle][item.KeyID] = key
		}
	}
	paths, _ := filepath.Glob("plugins/*.json")
	if len(paths) == 0 {
		fmt.Println("plugins/ absent (build artifacts not committed) — nothing to verify")
		return
	}
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			panic(err)
		}
		var doc map[string]any
		if err := json.Unmarshal(raw, &doc); err != nil {
			panic(err)
		}
		sigDoc, ok := doc["signature"].(map[string]any)
		if !ok {
			panic(path + ": missing signature")
		}
		keyID, _ := sigDoc["key_id"].(string)
		encoded, _ := sigDoc["sig"].(string)
		publisherID, _ := doc["publisher"].(string)
		if revoked[keyID] {
			panic(path + ": signed with revoked key " + keyID)
		}
		pub, ok := keys[publisherID][keyID]
		if !ok {
			panic(path + ": signing key " + keyID + " is not published under " + publisherID)
		}
		delete(doc, "signature")
		canonical, err := json.Marshal(doc)
		if err != nil {
			panic(err)
		}
		sig, err := base64.StdEncoding.DecodeString(encoded)
		if err != nil {
			panic(err)
		}
		if !ed25519.Verify(pub, canonical, sig) {
			panic(path + ": signature does not match the canonical manifest; re-sign it with the publisher seed")
		}
	}
	fmt.Printf("verified %d signatures\n", len(paths))
}
