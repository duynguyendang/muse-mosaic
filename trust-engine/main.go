package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	port := envOr("PORT", "8080")
	policyDir := envOr("POLICY_DIR", "./policies")
	taxonomyDir := envOr("TAXONOMY_DIR", "./taxonomy")

	engine, err := NewEngine(policyDir, taxonomyDir)
	if err != nil {
		logger.Error("cannot load trust-engine policies", "error", err)
		os.Exit(1)
	}
	userVersion, productVersion := engine.PolicyVersions()
	logger.Info(
		"trust-engine policies loaded",
		"policy_version_user", userVersion,
		"policy_version_product", productVersion,
		"adapter", AdapterState,
	)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", engine.handleHealth)
	mux.HandleFunc("POST /derive", engine.handleDerive)
	// /metrics re-runs Stage 03+04 over cached Stage 02 clusters. It shells
	// out to manglekit once per cluster, so it needs a much longer write
	// budget than /derive (which handles a single cluster).
	mux.HandleFunc("POST /metrics", engine.handleMetrics)
	handler := recoverPanics(logger, logRequests(logger, mux))

	server := &http.Server{
		Addr:              ":" + port,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      metricsWriteTimeout(),
		IdleTimeout:       60 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// SIGHUP reloads policies (e.g. after a new policy version is deployed).
	// A failed reload keeps the previous policies in force and is logged;
	// it never stops the server and never swaps in unvalidated policies.
	sighup := make(chan os.Signal, 1)
	signal.Notify(sighup, syscall.SIGHUP)
	go func() {
		for range sighup {
			if err := engine.ReloadPolicies(); err != nil {
				logger.Error("policy reload on SIGHUP failed; previous policies kept", "error", err)
				continue
			}
			userVersion, productVersion := engine.PolicyVersions()
			logger.Info(
				"policies reloaded on SIGHUP",
				"policy_version_user", userVersion,
				"policy_version_product", productVersion,
			)
		}
	}()

	serveErr := make(chan error, 1)
	go func() {
		logger.Info("trust-engine listening", "addr", server.Addr)
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serveErr <- err
		}
	}()

	select {
	case err := <-serveErr:
		logger.Error("trust-engine stopped unexpectedly", "error", err)
		os.Exit(1)
	case <-ctx.Done():
		logger.Info("trust-engine shutting down")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			logger.Error("graceful shutdown failed", "error", err)
			os.Exit(1)
		}
	}
}

type healthResponse struct {
	Status               string `json:"status"`
	Service              string `json:"service"`
	PolicyVersionUser    string `json:"policy_version_user"`
	PolicyVersionProduct string `json:"policy_version_product"`
	Adapter              string `json:"adapter"`
	ManglekitVersion     string `json:"manglekit_version"`
}

type failClosedResponse struct {
	DecisionID    string `json:"decision_id,omitempty"`
	EntityType    string `json:"entity_type,omitempty"`
	EntityID      string `json:"entity_id,omitempty"`
	PolicyVersion string `json:"policy_version,omitempty"`
	Verdict       string `json:"verdict"`
	ReasonCode    string `json:"reason_code"`
	Message       string `json:"message"`
	FailClosed    bool   `json:"fail_closed"`
}

func (e *Engine) handleHealth(w http.ResponseWriter, _ *http.Request) {
	userVersion, productVersion := e.PolicyVersions()
	writeJSON(w, http.StatusOK, healthResponse{
		Status:               "ok",
		Service:              "trust-engine",
		PolicyVersionUser:    userVersion,
		PolicyVersionProduct: productVersion,
		Adapter:              AdapterState,
		ManglekitVersion:     ManglekitVersion,
	})
}

// handleDerive validates, derives via manglekit, and returns golden facts
// with proof. Any failure fails closed with no golden facts.
func (e *Engine) handleDerive(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()

	var request DeriveRequest
	if err := decoder.Decode(&request); err != nil {
		writeJSON(w, http.StatusBadRequest, failClosedResponse{
			Verdict:    "denied",
			ReasonCode: "INVALID_JSON",
			Message:    "request is not valid derive JSON: " + err.Error(),
			FailClosed: true,
		})
		return
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		writeJSON(w, http.StatusBadRequest, failClosedResponse{
			Verdict:    "denied",
			ReasonCode: "INVALID_JSON",
			Message:    "request body must contain exactly one JSON object",
			FailClosed: true,
		})
		return
	}

	if err := e.ValidateDerive(request); err != nil {
		reason := "INVALID_REQUEST"
		var requestErr *RequestError
		if errors.As(err, &requestErr) {
			reason = requestErr.Code
		}
		writeJSON(w, http.StatusBadRequest, failClosedResponse{
			DecisionID:    request.DecisionID,
			EntityType:    request.EntityType,
			EntityID:      request.EntityID,
			PolicyVersion: request.PolicyVersion,
			Verdict:       "denied",
			ReasonCode:    reason,
			Message:       err.Error(),
			FailClosed:    true,
		})
		return
	}

	output, err := e.Derive(request)
	if err != nil {
		reason := "ENGINE_ERROR"
		status := http.StatusInternalServerError
		var requestErr *RequestError
		if errors.As(err, &requestErr) {
			reason = requestErr.Code
			if strings.HasPrefix(reason, "INVALID_") ||
				reason == "UNKNOWN_ENTITY_TYPE" ||
				reason == "UNKNOWN_POLICY_VERSION" ||
				reason == "ATTRIBUTE_NOT_ALLOWED" {
				status = http.StatusBadRequest
			}
		}
		writeJSON(w, status, failClosedResponse{
			DecisionID:    request.DecisionID,
			EntityType:    request.EntityType,
			EntityID:      request.EntityID,
			PolicyVersion: request.PolicyVersion,
			Verdict:       "escalated",
			ReasonCode:    reason,
			Message:       err.Error() + "; fail closed, no golden facts",
			FailClosed:    true,
		})
		return
	}

	writeJSON(w, http.StatusOK, output)
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func recoverPanics(logger *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if recovered := recover(); recovered != nil {
				logger.Error("panic in trust-engine handler", "path", r.URL.Path, "panic", recovered)
				writeJSON(w, http.StatusInternalServerError, failClosedResponse{
					Verdict:    "denied",
					ReasonCode: "INTERNAL_ERROR",
					Message:    "trust-engine failed closed after an internal error",
					FailClosed: true,
				})
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func logRequests(logger *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		next.ServeHTTP(w, r)
		logger.Info(
			"request",
			"method", r.Method,
			"path", r.URL.Path,
			"duration_ms", time.Since(started).Milliseconds(),
		)
	})
}

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

// metricsWriteTimeout bounds a whole profile evaluation. Precomputing three
// profiles over ~247 clusters takes tens of seconds; the budget scales with
// the configured worker count so a wide fan-out is not cut off mid-run.
func metricsWriteTimeout() time.Duration {
	base := 120 * time.Second
	if v := os.Getenv("METRICS_TIMEOUT_SECONDS"); v != "" {
		var secs int
		if _, err := fmt.Sscanf(v, "%d", &secs); err == nil && secs > 0 {
			return time.Duration(secs) * time.Second
		}
	}
	return base
}

// handleMetrics evaluates cached Stage 02 clusters under one policy profile
// and returns aggregate Stage 03/04 metrics. It never writes to the ledger:
// a policy preview must not become a publish.
func (e *Engine) handleMetrics(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 64<<20)) // 64 MiB of cached clusters
	if err != nil {
		writeJSON(w, http.StatusBadRequest, failClosedResponse{Verdict: "escalated", ReasonCode: "INVALID_JSON", Message: "cannot read body; fail closed", FailClosed: true})
		return
	}
	var req MetricsRequest
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, failClosedResponse{Verdict: "escalated", ReasonCode: "INVALID_JSON", Message: "request is not valid metrics JSON: " + err.Error() + "; fail closed", FailClosed: true})
		return
	}
	resp, err := e.EvaluateMetrics(req)
	if err != nil {
		var reqErr *RequestError
		if errors.As(err, &reqErr) {
			status := http.StatusInternalServerError
			if strings.HasPrefix(reqErr.Code, "INVALID_") || reqErr.Code == "UNKNOWN_POLICY_VERSION" || reqErr.Code == "UNKNOWN_TAXONOMY_VERSION" {
				status = http.StatusBadRequest
			}
			writeJSON(w, status, failClosedResponse{Verdict: "escalated", ReasonCode: reqErr.Code, Message: reqErr.Message + "; fail closed", FailClosed: true})
			return
		}
		writeJSON(w, http.StatusInternalServerError, failClosedResponse{Verdict: "escalated", ReasonCode: "INTERNAL_ERROR", Message: err.Error() + "; fail closed", FailClosed: true})
		return
	}
	writeJSON(w, http.StatusOK, resp)
}
