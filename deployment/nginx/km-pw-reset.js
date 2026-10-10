(function(){
  if (window.location.pathname.indexOf('/login') === -1) return;

  var observer = new MutationObserver(function(){
    if (document.getElementById("km-pw-reset")) return;
    var form = document.querySelector("[data-test-subj=loginForm]") ||
               document.querySelector("form");
    if (form) {
      var d = document.createElement("div");
      d.id = "km-pw-reset";
      d.style.cssText = "text-align:center;margin-top:16px;";
      d.innerHTML = "<a href=\"/reset-password/\" style=\"color:#07C;font-size:14px;text-decoration:none;\">Forgot your password?</a>";
      form.appendChild(d);
    }
  });
  observer.observe(document.documentElement, {childList:true, subtree:true});
})();
