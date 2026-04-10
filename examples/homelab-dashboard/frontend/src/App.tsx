import { useEffect, useState } from 'react'
import './App.css'

interface Service {
  name: string
  href: string
  description: string
  icon?: string
}

interface ServiceGroup {
  name: string
  services: Service[]
}

function App() {
  const [serviceGroups, setServiceGroups] = useState<ServiceGroup[]>([])
  const [loading, setLoading] = useState(true)
  const [rawResponse, setRawResponse] = useState<any>(null)
  const [aiAnalysis, setAiAnalysis] = useState<string | null>(null)
  const [analyzing, setAnalyzing] = useState(false)
  const [activeModel, setActiveModel] = useState<string | null>(null)

  const fetchAnalysis = async () => {
    setAnalyzing(true)
    setAiAnalysis(null)
    
    // Quick pre-fetch to display the active model name right away
    try {
      const llmRes = await fetch('/api/llm')
      const llmJson = await llmRes.json()
      if (llmJson.models && llmJson.models.length > 0) {
        setActiveModel(llmJson.models[0].id)
      }
    } catch(e) {}

    try {
      const res = await fetch('/api/analyze', { method: 'POST' })
      const json = await res.json()
      if (res.ok && json.status === 'success') {
        setAiAnalysis(json.analysis)
        if (json.model) setActiveModel(json.model)
      } else {
        setAiAnalysis(`Analysis failed: ${json.detail || 'Unknown error'}`)
      }
    } catch (e: any) {
      setAiAnalysis(`Error contacting AI: ${e.message}`)
    } finally {
      setAnalyzing(false)
    }
  }

  const fetchData = async () => {
    setLoading(true)
    setAiAnalysis(null)
    try {
      const res = await fetch('/api/homepage')
      const text = await res.text()
      let json;
      try {
        json = JSON.parse(text)
      } catch (parseError: any) {
        throw new Error(`JSON Parse Error: ${parseError.message}\n\nReceived Text: ${text.substring(0, 500)}`)
      }
      setRawResponse(json)
      
      // Extract the services array from the Next.js fallback payload
      const fallbackData = json.data?.fallback || {}
      
      // Look for the services key dynamically since Next.js SWR keys can vary
      const servicesKey = Object.keys(fallbackData).find(key => key.includes('/api/services'))
      
      if (servicesKey) {
        setServiceGroups(fallbackData[servicesKey] || [])
      } else {
        setServiceGroups([])
      }
    } catch (e: any) {
      console.error('Failed to fetch dashboard data', e)
      setRawResponse({ error: e.message || String(e) })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
  }, [])

  return (
    <div className="dashboard-container">
      <header className="dashboard-header">
        <div>
          <h1>Homelab Orchestration</h1>
          <p className="subtitle">Infrastructure & Services</p>
        </div>
        <div className="header-actions">
          <button onClick={fetchAnalysis} className="ai-btn" disabled={analyzing || loading}>
            {analyzing ? 'Analyzing...' : 'Run AI Anomaly Check'}
          </button>
          <button onClick={fetchData} className="refresh-btn" disabled={loading}>
            {loading ? 'Refreshing...' : 'Refresh Live Data'}
          </button>
        </div>
      </header>
      
      <main className="dashboard-content">
        {(aiAnalysis || analyzing) && (
          <section className="ai-analysis-panel">
            <h2><span className="ai-icon">✨</span> AI Infrastructure Analysis</h2>
            {analyzing ? (
              <p className="analyzing-text">
                {activeModel ? `[${activeModel}]` : 'The largest local model'} is analyzing the infrastructure state...
              </p>
            ) : (
              <div className="analysis-content">
                {activeModel && <p style={{color: '#8b5cf6', fontSize: '0.85rem', marginBottom: '1rem', marginTop: 0}}>Model: {activeModel}</p>}
                {aiAnalysis}
              </div>
            )}
          </section>
        )}

        {loading && serviceGroups.length === 0 ? (
          <div className="loading-state">Loading infrastructure data...</div>
        ) : serviceGroups.length > 0 ? (
          serviceGroups.map((group, index) => (
            <section key={index} className="service-group">
              <h2 className="group-title">{group.name}</h2>
              <div className="service-grid">
                {group.services?.map((service, sIndex) => (
                  <a 
                    key={sIndex} 
                    href={service.href} 
                    target="_blank" 
                    rel="noopener noreferrer" 
                    className="service-card"
                  >
                    <div className="service-header">
                      {service.icon ? (
                        <img 
                          src={`/api/icon?name=${encodeURIComponent(service.icon)}`} 
                          alt="icon" 
                          className="service-icon"
                          onError={(e) => {
                            const target = e.target as HTMLElement;
                            target.style.display = 'none';
                            const placeholder = document.createElement('span');
                            placeholder.className = 'service-icon-placeholder';
                            target.parentNode?.insertBefore(placeholder, target as Node);
                          }}
                        />
                      ) : (
                        <span className="service-icon-placeholder"></span>
                      )}
                      <h3 className="service-name">{service.name}</h3>
                    </div>
                    {service.description && (
                      <p className="service-description">{service.description}</p>
                    )}
                  </a>
                ))}
              </div>
            </section>
          ))
        ) : (
          <div className="error-state">
            <p>Failed to parse services from Homepage data.</p>
            <pre style={{ textAlign: 'left', fontSize: '12px', marginTop: '1rem', overflow: 'auto' }}>
              {JSON.stringify(rawResponse, null, 2)}
            </pre>
          </div>
        )}
      </main>
    </div>
  )
}

export default App