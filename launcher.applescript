-- Abre a interface web do ytm-dl no navegador e mantem o servidor vivo
-- enquanto esta janela estiver aberta.
on run
	set caminhoApp to POSIX path of (path to me)
	set raiz to do shell script "dirname " & quoted form of (text 1 thru -2 of caminhoApp)
	set py to raiz & "/.venv/bin/python"
	set porta to "8756"
	
	try
		do shell script "test -x " & quoted form of py
	on error
		display dialog "Nao encontrei o ambiente Python em:" & return & return & py & return & return & "Rode uma vez no Terminal, dentro da pasta do projeto:" & return & "python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt" buttons {"OK"} default button 1 with icon stop with title "ytm-dl"
		return
	end try
	
	-- Ja esta no ar? So traz o navegador de volta.
	try
		do shell script "curl -sf -o /dev/null --max-time 1 http://127.0.0.1:" & porta & "/api/config"
		do shell script "open http://127.0.0.1:" & porta & "/"
		return
	end try
	
	set pid to do shell script "cd " & quoted form of raiz & " && nohup " & quoted form of py & " server.py --port " & porta & " --open > /tmp/ytm-dl.log 2>&1 & echo $!"
	
	display dialog "O ytm-dl abriu no seu navegador." & return & return & "Deixe esta janela aberta enquanto baixa. Clique em Encerrar quando terminar." buttons {"Encerrar"} default button 1 with title "ytm-dl" with icon note
	do shell script "kill " & pid & " 2>/dev/null; true"
end run
