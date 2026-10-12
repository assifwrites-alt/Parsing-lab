<?php
// Is file ko config.php naam se copy karo aur token badlo (lambi random string, 24+ akshar).
return [
  'token'    => 'CHANGE_ME',
  'data_dir' => __DIR__ . '/data',   // uploads/result yahin rakhe jate hain (andar .htaccess se band rehta hai)
  // Website par Claude agent ke liye (optional). console.anthropic.com se API key; ye file kabhi GitHub par na jaye.
  // 'anthropic_key'   => 'sk-ant-...',
  // 'anthropic_model' => 'claude-sonnet-5-5',
];
