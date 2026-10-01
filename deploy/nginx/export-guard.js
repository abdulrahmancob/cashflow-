(function () {
  var orig = window.fetch
  if (!orig) return
  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || ''
    if (url.indexOf('/eligibility/items/export') === -1 || url.indexOf('export-job') !== -1) {
      return orig.apply(this, arguments)
    }
    return startExport(url, init, input)
  }

  function credentialsOf(init, input) {
    if (init && init.credentials) return init.credentials
    if (input && input.credentials) return input.credentials
    return 'include'
  }

  function startExport(url, init, input) {
    var q = url.indexOf('?')
    var path = q === -1 ? url : url.slice(0, q)
    var query = q === -1 ? '' : url.slice(q)
    var jobBase = path.replace('/items/export', '/items/export-job')
    var creds = credentialsOf(init, input)
    return orig.call(window, jobBase + query, { method: 'POST', credentials: creds }).then(function (response) {
      if (!response.ok) return Promise.reject(new Error('export failed'))
      return response.json().then(function (job) {
        if (!job || !job.id) return Promise.reject(new Error('export failed'))
        return poll(jobBase + '/' + job.id, creds)
      })
    })
  }

  function poll(statusUrl, creds) {
    return orig.call(window, statusUrl, { credentials: creds }).then(function (response) {
      if (response.status === 202) {
        return new Promise(function (resolve) {
          setTimeout(resolve, 2000)
        }).then(function () {
          return poll(statusUrl, creds)
        })
      }
      if (!response.ok) return Promise.reject(new Error('export failed'))
      return response
    })
  }
})()
